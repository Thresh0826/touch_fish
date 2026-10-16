"""Only this entry assembles native ports; no original login/workflow imports."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
import math
from pathlib import Path
import sys
import time
import uuid

from errors import WDMSError
from moyu_tools.engine import Engine
from moyu_tools.direct_copy import inspect_target
from moyu_tools.model import Plan, Policy, digest, fail
from moyu_tools.settings import inventory_lock_path, load_json, profile_digest, state_root, task_lock_path, validate_profile
from moyu_tools.store import Store
from moyu_tools.sheet import SheetPlan
from services.event_log import error_info, record
from services.process_supervisor import supervise
from services.status_manager import atomic_json_write
from services.task_lock import TaskLock

PROJECT = Path(__file__).resolve().parents[1]
ENTRY = PROJECT/"scripts"/"MoyuTools.py"


def validate_plan_binding(plan, profile):
    validate_profile(profile)
    if isinstance(plan, SheetPlan) != (profile["schema"] >= 2):
        fail("MOYU_PLAN_BINDING_MISMATCH", "表格逐行任务需要新版合同编号定位；旧日期任务和旧定位保留，请重新定位并新建表格任务。")


def copy_mode(profile):
    return "direct" if profile["schema"] == 3 else None


def make_request(config, profile, *, trial):
    plan, policy = Plan.parse(config["plan"]), Policy(**config["policy"])
    validate_plan_binding(plan, profile)
    if not isinstance(config.get("output"), str) or not config["output"].strip():
        fail("MOYU_OUTPUT_INVALID", "请选择已经建立好的本地目标文件夹。")
    output = Path(config["output"]).expanduser().resolve()
    if not output.is_dir() or output.is_symlink():
        fail("MOYU_OUTPUT_INVALID", "请选择已经建立好的本地目标文件夹。")
    task = Path(config["task"]) if config.get("task") else state_root()/"tasks"/uuid.uuid4().hex
    task = task.resolve()
    if (task/"progress.json").exists() and copy_mode(profile) == "direct":
        prior = load_json(task/"progress.json")
        if prior.get("identity", {}).get("copy_mode") != "direct":
            fail("MOYU_COPY_MODE_CHANGED", "原任务使用旧暂存流程，记录已保留；请点击新建任务后使用直接复制，旧任务可用旧包继续。")
    task.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    request = {"schema": 1, "request_id": request_id, "task": str(task), "plan": plan.document(),
               "policy": asdict(policy), "output": str(output), "profile": profile, "trial": bool(trial),
               "result": str(task/(request_id+".result.json")), "cancel": str(task/(request_id+".cancel")),
               "controller_pid": os.getpid()}
    request["watch"] = str(task/(request_id+".watch.json"))
    path = task/(request_id+".request.json")
    atomic_json_write(path, request)
    return path, request


def verify_child(code, request):
    result_path = Path(request["result"])
    if not result_path.exists():
        fail("MOYU_WORKER_UNCONFIRMED", "执行进程已停止但缺少本轮结果凭证；进度保留，未认定成功。")
    result = load_json(result_path)
    progress = Path(request["task"])/"progress.json"
    if result.get("request_id") != request["request_id"] or result.get("request_sha256") != digest(request):
        fail("MOYU_WORKER_UNCONFIRMED", "执行凭证与本轮请求或进度不一致，未认定成功。")
    if type(code) is not int or code != 0 or result.get("ok") is not True:
        failure = result.get("error") or {}
        fail(failure.get("error_code", "MOYU_WORKER_FAILED"), failure.get("error", "本轮已停住，请查看运行日志。"))
    if not progress.exists() or result.get("progress_sha256") != hashlib.sha256(progress.read_bytes()).hexdigest():
        fail("MOYU_WORKER_UNCONFIRMED", "完成凭证没有对应本轮进度，未认定成功。")
    value = Store(request["task"]).load_or_create(Plan.parse(request["plan"]), request["output"], profile_digest(request["profile"]),
                                                 request["profile"]["wdms"]["conditions_hash"], copy_mode(request["profile"]))
    if result.get("status") != value.get("status") or result["status"] not in ("completed", "trial_passed", "trial_empty", "trial_pending"):
        fail("MOYU_WORKER_UNCONFIRMED", "完成状态没有得到进度记录确认。")
    if (result["status"] == "completed" and any(row["phase"] not in ("done", "empty") for row in value["segments"])):
        fail("MOYU_WORKER_UNCONFIRMED", "仍有未完成区间，不能认定整批完成。")
    if result["status"] == "trial_passed" and value["trial_passed"] is not True:
        fail("MOYU_WORKER_UNCONFIRMED", "试跑没有完整文件凭证，未解锁完整范围。")
    return result["status"]


def run_child(path, request):
    policy, plan = Policy(**request["policy"]), Plan.parse(request["plan"])
    folder = Path(request["task"])
    log = folder/"events.jsonl"
    stderr = folder/(request["request_id"]+".console.txt")
    cancel = Path(request["cancel"])
    watched = {"token": None, "started": time.monotonic(), "seconds": policy.preparation, "expired": False}
    def stopped():
        if cancel.exists():
            return True
        watch_file = Path(request["watch"])
        if watch_file.exists():
            value = load_json(watch_file)
            seconds = value.get("seconds")
            allowed = {policy.preparation, policy.query_timeout, policy.search_timeout, policy.copy_timeout}
            if (value.get("request_id") != request["request_id"] or not isinstance(value.get("token"), str)
                    or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds not in allowed):
                fail("MOYU_WATCH_INVALID", "本轮步骤监督凭据不匹配，已停止本轮进程。")
            if value["token"] != watched["token"]:
                watched.update(token=value["token"], started=time.monotonic(), seconds=seconds)
        if time.monotonic()-watched["started"] >= watched["seconds"]:
            watched["expired"] = True
            return True
        return False
    code = supervise([sys.executable, "-I", "-B", str(ENTRY), "--worker", str(path)], log,
                     policy.budget(len(list(plan.segments()))), cancelled=stopped,
                     stderr_path=stderr, cwd=PROJECT)
    if cancel.exists():
        # A deliberate stop is non-terminal business state, not a missing-success
        # receipt. The last atomic phase remains authoritative for reconciliation.
        return "stopped"
    if watched["expired"]:
        record(log, "stage_hard_timeout", request_id=request["request_id"], seconds=watched["seconds"],
               error_code="MOYU_STAGE_HARD_TIMEOUT")
        fail("MOYU_STAGE_HARD_TIMEOUT", "本步骤超过配置上限，已停止脚本并保留阶段；WDMS查询或资源管理器复制可能仍在进行。")
    return verify_child(code, request)


def worker(path):
    request = load_json(path)
    folder = Path(request["task"])
    log = folder/"events.jsonl"
    store, bridge, parent = None, None, None
    result = {"request_id": request["request_id"], "request_sha256": digest(request), "ok": False}
    try:
        if sys.platform != "win32":
            fail("MOYU_WINDOWS_REQUIRED", "现场运行仅支持Windows，本轮未操作WDMS或远程系统。")
        # Import native code only in the child, never in GUI routing/inspection.
        from moyu_tools.bridge import Bridge, ParentHandle
        from moyu_tools.native import Driver, LocalQuery, RemoteFiles
        with TaskLock(task_lock_path()), TaskLock(inventory_lock_path()):
            profile = validate_profile(request["profile"])
            plan, policy = Plan.parse(request["plan"]), Policy(**request["policy"])
            validate_plan_binding(plan, profile)
            def context_started(seconds):
                atomic_json_write(Path(request["watch"]), {"request_id": request["request_id"],
                                                          "token": uuid.uuid4().hex, "seconds": seconds})
            context_started(policy.preparation)
            store = Store(folder)
            store.load_or_create(plan, request["output"], profile_digest(profile), profile["wdms"]["conditions_hash"], copy_mode(profile))
            parent = ParentHandle(request["controller_pid"])
            bridge = Bridge(log)
            driver = Driver(bridge, log)
            engine = Engine(plan, policy, store, LocalQuery(driver, profile), RemoteFiles(driver, profile),
                            cancelled=lambda: Path(request["cancel"]).exists() or not parent.alive(),
                            emit=lambda event, **data: record(log, event, **data), on_context=context_started)
            result["status"] = engine.run(trial=request["trial"])
            result["ok"] = True
    except Exception as error:
        info = error_info(error)
        record(log, "moyu_stopped", **info)
        result["error"] = info
        if store is not None and store.value is not None:
            store.pause(WDMSError(info["error_code"], info["error"]))
    finally:
        if bridge is not None:
            bridge.close()
        if parent is not None:
            parent.close()
    progress = folder/"progress.json"
    result["progress_sha256"] = hashlib.sha256(progress.read_bytes()).hexdigest() if progress.exists() else None
    atomic_json_write(Path(request["result"]), result)
    return 0 if result["ok"] else 2


def rearm(config, profile):
    """Explicit human recovery only; no automation is invoked by this operation."""
    with TaskLock(task_lock_path()):
        plan = Plan.parse(config["plan"])
        validate_plan_binding(plan, profile)
        store = Store(config["task"])
        store.load_or_create(plan, config["output"], profile_digest(profile), profile["wdms"]["conditions_hash"], copy_mode(profile))
        row = next((x for x in store.value["segments"] if x["phase"] not in ("done", "empty")), None)
        if row is None:
            return
        index = store.value["segments"].index(row)
        if copy_mode(profile) == "direct" and row["phase"] in ("copy_intent", "copying"):
            target = Path(config["output"])/row["selected"]["name"]
            if target.exists():
                try:
                    inspect_target(target)
                except WDMSError as error:
                    if error.code != "MOYU_COPY_PENDING":
                        raise
                else:
                    fail("MOYU_COPY_PENDING", "目标ZIP已完整，请用继续核实并保存进度；未重复粘贴。")
                archive = target.parent/("recovery_"+store.value["run_id"][:8]+"_"+uuid.uuid4().hex[:8]+"_"+target.name[-170:])
                store.update(index, row["phase"], rearm_pending=True,
                             recovery_backups=row.get("recovery_backups", [])+[str(archive)])
                target.rename(archive)
            store.update(index, "queried", rearm_pending=False, destination_absent=False)
            store.value.update(status="ready", error=None)
            store.commit()
            return
        if row["phase"] in ("copy_intent", "copying", "verified"):
            stage = store.stage_folder(index)
            if row["phase"] == "verified":
                fail("MOYU_COPY_PENDING", "已经生成文件校验凭证，请使用继续完成发布。")
            if stage.exists() and list(stage.iterdir()):
                archive = stage.with_name("recovery_"+stage.name+"_"+uuid.uuid4().hex)
                backups = row.get("recovery_backups", [])+[str(archive)]
                store.update(index, row["phase"], recovery_backups=backups, rearm_pending=True)
                # Never remove a partial copy. A locked/in-flight directory cannot
                # be silently discarded; the operator explicitly confirmed it ended.
                stage.rename(archive)
            store.update(index, "queried", rearm_pending=False)
        elif row["phase"] in ("query_intent", "querying"):
            store.update(index, "pending", busy_seen=False)
        else:
            store.update(index, "queried" if row.get("number") else "pending")
        store.value.update(status="ready", error=None)
        store.commit()


def adopt_binding(config, profile):
    """Human-confirmed rebind keeps task receipts; requires identical conditions."""
    validate_plan_binding(Plan.parse(config["plan"]), profile)
    with TaskLock(task_lock_path()):
        store = Store(config["task"])
        prior = load_json(store.path)
        if prior["identity"].get("conditions") != profile["wdms"]["conditions_hash"]:
            fail("MOYU_CONDITIONS_CHANGED", "其他筛选条件与旧任务不同，未沿用进度；请恢复旧条件或新建任务。")
        old_binding = prior["identity"]["binding"]
        store.load_or_create(Plan.parse(config["plan"]), config["output"], old_binding,
                             profile["wdms"]["conditions_hash"], copy_mode(profile))
        for index, row in enumerate(store.value["segments"]):
            if row["phase"] in ("done", "empty"):
                continue
            if row["phase"] in ("query_intent", "querying"):
                store.update(index, "pending", busy_seen=False, initial_wait_seconds=0)
            elif row["phase"] in ("searching", "selected"):
                store.update(index, "queried")
            break
        store.rebind(profile_digest(profile))
        record(store.folder/"events.jsonl", "binding_reconfirmed", completed=sum(
            x["phase"] in ("done", "empty") for x in store.value["segments"]))
