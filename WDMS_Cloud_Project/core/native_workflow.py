"""One-click orchestration over typed stage and collection ports."""
from __future__ import annotations
from typing import Optional
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from core.step_executor import Step, StepExecutor
from core.collection_contracts import CollectionRequest, StageRequest
from core.query_policy import DEFAULT_QUERY_POLICY
from core.login_policy import DEFAULT_LOGIN_READINESS
from errors import WDMSError
from services import owner_catalog
from services.workflow_evidence import (stage_events, stage_failure, copy_collection_evidence,
                                       FATAL_STAGE_EVENTS)


def verify_report(report, owners, report_date):
    # Keep the optional report dependency at the report boundary so the
    # desktop preflight can explain a missing openpyxl before any input.
    from services.trial_excel_reporter import verify_report as verify
    return verify(report, owners, report_date)

@dataclass(frozen=True)
class WorkflowDependencies:
    prepare_session: Callable
    navigate: Callable
    capture_owners: Callable
    collect: Callable
    load_settings: Callable
    emit: Callable
    clock: Optional[Callable] = None


def validated_owners(names):
    if (not isinstance(names, (list, tuple)) or len(names) != 24
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(set(names)) != 24):
        raise WDMSError("WORKFLOW_OWNERS_INVALID", "真实货主名单须包含24个不同的非空名称；未启动WDMS。")
    return tuple(names)


def validate_limit(limit):
    if type(limit) is not int or limit not in (3, 24):
        raise WDMSError("WORKFLOW_LIMIT_INVALID", "一键流程只支持前3个或全部24个货主。")


def call_runner(runner, arguments):
    try:
        return runner(arguments)
    except SystemExit as error:
        raise WDMSError("WORKFLOW_STAGE_EXITED", "阶段提前退出，未继续后续步骤；详情见本轮控制台记录。") from error


def run_stages(folder, output, limit, notice, dependencies, run_context, *, cancel_file=None, owners=None, config=None):
    report_date = run_context.report_date
    folder = Path(folder)
    output = Path(output)
    log = folder / "flow.jsonl"
    cancel_path = Path(cancel_file) if cancel_file is not None else folder / "cancel.request"
    cancel_path = cancel_path.resolve()
    manifest = folder / "collection_result.json"
    confirmed_report = {}
    output_root = output.resolve()
    validate_limit(limit)
    if cancel_path.exists():
        raise WDMSError("TASK_CANCELLED", "任务已取消，未启动WDMS。")
    if any((folder / name).exists() for name in
           ("login", "navigation", "owners", "collection", "owners_snapshot.json", "collection_result.json")):
        raise WDMSError("WORKFLOW_ARTIFACTS_EXIST", "本轮目录已有阶段文件，请重新启动生成新任务目录；未复用旧结果。")
    names = validated_owners(owners) if owners is not None else None
    automatic = names is None
    stage_count = 4 if automatic else 3
    owner_file = folder / "owners_snapshot.json"
    owner_digest = None
    if not automatic:
        owner_data = (json.dumps({"owners": names}, ensure_ascii=False) + "\n").encode("utf-8")
        with owner_file.open("xb") as handle:
            handle.write(owner_data)
        owner_digest = hashlib.sha256(owner_data).hexdigest()
        dependencies.emit(log, "workflow_inputs_snapshotted", owner_count=len(names), selected_count=limit,
                     owners_file=owner_file.name, owners_sha256=owner_digest)
    collection_folders = set()

    def stage_step(name, runner, directory, marker, timeout_seconds, index, *, reuse_session=False, snapshot=None):
        def action(context):
            directory.mkdir()
            notice.update(index, stage_count, name)
            dependencies.emit(log, "workflow_stage_started", stage=name)
            return call_runner(runner, StageRequest(directory, context.deadline, cancel_path,
                                                   snapshot, reuse_session))

        def verify(code):
            events = stage_events(directory)
            fatal = any(event.get("error_code") and event.get("event") in FATAL_STAGE_EVENTS for event in events)
            if type(code) is not int or code != 0 or fatal:
                raise stage_failure(events, "WORKFLOW_STAGE_FAILED", f"{name}未完成，详情见本轮日志。")
            if sum(event.get("event") == marker for event in events) != 1:
                raise WDMSError(
                    "WORKFLOW_STAGE_UNCONFIRMED",
                    f"{name}没有成功确认记录，未进入下一步。",
                )
            dependencies.emit(log, "workflow_stage_complete", stage=name)
            return True

        return Step(name, action, verify, timeout_seconds)

    login_dir = folder / "login"
    navigation_dir = folder / "navigation"
    steps = [
        stage_step("准备WDMS会话", dependencies.prepare_session, login_dir, "automatic_login_complete", DEFAULT_LOGIN_READINESS.workflow_seconds, 1,
                   reuse_session=True),
        stage_step("自动导航", dependencies.navigate, navigation_dir, "automatic_navigation_complete", 245, 2),
    ]
    if automatic:
        steps.append(stage_step("读取货主名单", dependencies.capture_owners, folder / "owners",
                                "owner_list_capture_complete", 80, 3,
                                snapshot=owner_file))

    def collect(context):
        collection_folders.update(path.resolve() for path in output.glob("native_trial_*") if path.is_dir())
        owner_catalog.read_plan(owner_file, expected_sha256=owner_digest)
        notice.update(stage_count, stage_count, f"采集 {limit} 个货主并保存表格")
        dependencies.emit(log, "workflow_stage_started", stage="库存采集", owner_count=limit)
        request = CollectionRequest(limit, output, report_date, folder.name, owner_file,
            owner_digest, managed_notices=True, workflow_progress=notice.progress,
            result_manifest=manifest, cancel_file=cancel_path, execution_deadline=context.deadline)
        try:
            return call_runner(dependencies.collect, request)
        finally:
            new_folders = {path.resolve() for path in output.glob("native_trial_*") if path.is_dir()} - collection_folders
            if len(new_folders) == 1:
                copy_collection_evidence(next(iter(new_folders)), folder/'collection', log, emit=dependencies.emit)

    def verify_collection(code):
        new_folders = {path.resolve() for path in output.glob("native_trial_*") if path.is_dir()} - collection_folders
        if type(code) is not int or code != 0:
            events = stage_events(next(iter(new_folders))) if len(new_folders) == 1 else []
            raise stage_failure(events, "WORKFLOW_COLLECTION_FAILED",
                                "库存采集未完整完成，详情见本轮控制台与采集证据目录。")
        try:
            result = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("manifest must be an object")
            report_value = result.get("report")
            row_count = result.get("row_count")
            if not isinstance(report_value, str) or not report_value.strip():
                raise ValueError("report path is empty")
            if type(row_count) is not int or row_count != limit or result.get("requires_manual_review") is not True:
                raise ValueError("row count is invalid")
            if result.get("report_date") != report_date.isoformat() or result.get("run_id") != folder.name:
                raise ValueError("report context differs")
            report = Path(report_value).resolve()
            if (not report.is_relative_to(output_root) or not report.is_file()
                    or report.suffix.lower() != ".xlsx" or len(new_folders) != 1
                    or report.parent not in new_folders):
                raise ValueError("report is outside output or missing")
        except Exception as error:
            raise WDMSError(
                "WORKFLOW_REPORT_UNCONFIRMED",
                "报表路径、货主数量或结果清单无法确认。",
            ) from error
        verify_report(report, names[:limit], report_date)
        confirmed_report["path"] = report
        dependencies.emit(log, "workflow_stage_complete", stage="库存采集")
        return True

    def emit(event, fields):
        dependencies.emit(log, event, **fields)

    executor = StepExecutor(
        cancelled=lambda: cancel_path.exists(),
        emit=emit,
        clock=dependencies.clock or time.monotonic,
    )
    executor.run(steps)
    if automatic:
        config = config or dependencies.load_settings()
        if config is None:
            raise WDMSError("SETTINGS_MISSING", "读取名单后本机配置不可用，未开始查询。")
        try:
            names, owner_digest = owner_catalog.prepare_collection(
                owner_file, config, cancelled=lambda:cancel_path.exists())
        except WDMSError as error:
            dependencies.emit(log, "step_failed", step="货主名单校验", status="cancelled" if error.code=="TASK_CANCELLED" else "failed",
                         elapsed_seconds=None, error_code=error.code)
            raise
        dependencies.emit(log, "owners_plan_ready", owner_count=len(names),
                     fingerprint=owner_catalog.fingerprint(names), owners_sha256=owner_digest,
                     manual_review="after_excel_export")
    executor.run([Step("库存采集", collect, verify_collection, DEFAULT_QUERY_POLICY.collection_seconds(limit))])
    report = confirmed_report["path"]
    dependencies.emit(
        log,
        "automatic_workflow_complete",
        owner_count=limit,
        report=str(report),
        requires_manual_review=True,
        result_owner_independently_verified=False,
    )
    return report
