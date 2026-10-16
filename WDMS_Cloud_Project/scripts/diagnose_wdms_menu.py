"""Bounded, read-only MSAA menu diagnostics for an already-open WDMS home."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import tempfile
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from desktop_demo.login import LoginNativeWindows, pid_matches
from desktop_demo.settings import load_settings, settings_path, validate_executable
from errors import WDMSError
from scripts import check_wdms_window as probe
from scripts.check_wdms_startup import find_stage_window
from adapters.native_windows import message_policy


VERSION = "msaa-menu-probe-v5-fair-coverage"
TARGET_NAMES = frozenset(("在库管理", "在库查询", "在库商品检索"))
ALLOWED_NAMES = TARGET_NAMES | {"Tsp_顶部子系统菜单"}
HOME_MARKER = "用户密码修改"
WINDOW_CLASS_PREFIX = "WindowsForms10.Window.8."
STATIC_CLASS_PREFIX = "WindowsForms10.STATIC."
MAX_DISCOVERY_NODES = 96
MAX_DISCOVERY_DEPTH = 3
# Scan up to eight levels for home confirmation; MSAA candidates remain limited to three.
MAX_HOME_CONFIRMATION_DEPTH = 8
MAX_DISCOVERY_SECONDS = 3.0
MAX_CONTAINERS = 16
MAX_MSAA_NODES = 128
ALLOWED_MSAA_ROLES = frozenset((2, 10, 11, 12, 16, 20, 22, 30, 43))


def _fail(code: str, message: str) -> WDMSError:
    return WDMSError(code, message)


def _is_plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_static(class_name: str) -> bool:
    return class_name == "STATIC" or class_name.startswith(STATIC_CLASS_PREFIX)


def _read_visible_static_label(api, node: dict):
    """Read one visible STATIC caption without persisting its contents."""
    if not node.get("visible") or not _is_static(node.get("class_name", "")):
        return None
    user32 = getattr(api, "user32", None)
    if user32 is None:
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    result = ctypes.c_size_t()
    try:
        ok = user32.SendMessageTimeoutW(
            node["hwnd"], 0x000D, len(buffer),
            ctypes.cast(buffer, ctypes.c_void_p).value, 0x23, message_policy(api).text_read_ms,
            ctypes.byref(result),
        )
    except Exception:
        return None
    if not ok or result.value >= len(buffer) - 1:
        return None
    return buffer.value


def select_candidates(nodes, root):
    """Select visible same-process WinForms host windows in stable order."""
    root_hwnd = root.get("hwnd")
    root_pid = root.get("pid")
    candidates = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        hwnd, pid = node.get("hwnd"), node.get("pid")
        class_name = node.get("class_name")
        rect = node.get("rect")
        depth = node.get("depth")
        if (not _is_plain_int(hwnd) or hwnd == root_hwnd
                or not _is_plain_int(pid) or pid != root_pid
                or node.get("visible") is not True
                or not isinstance(class_name, str)
                or not class_name.startswith(WINDOW_CLASS_PREFIX)):
            continue
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or any(not _is_plain_int(value) for value in rect)
                or not _is_plain_int(depth) or depth < 1):
            raise _fail("MENU_CONTAINER_METADATA_INVALID", "菜单容器窗口元数据不完整，停止诊断。")
        if depth > MAX_DISCOVERY_DEPTH:
            continue
        candidates.append({
            "hwnd": hwnd,
            "class_name": class_name,
            "rect": list(rect),
            "depth": depth,
        })

    candidates.sort(key=lambda item: (item["depth"], item["rect"][1], item["rect"][0]))
    unique = []
    seen = set()
    for item in candidates:
        if item["hwnd"] in seen:
            continue
        seen.add(item["hwnd"])
        unique.append(item)
    if not unique:
        raise _fail("MENU_CONTAINER_NOT_FOUND", "没有找到可见的 WinForms 菜单容器。")
    if len(unique) > MAX_CONTAINERS:
        raise _fail("MENU_CONTAINER_LIMIT", "菜单容器超过 16 个，停止诊断且不截断结果。")
    return unique


def _atomic_write_request(path: Path, request: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix="request_", suffix=".tmp", dir=str(path.parent))
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(request, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_request_shape(request) -> dict:
    if not isinstance(request, dict) or set(request) != {
            "schema_version", "root_hwnd", "pid", "containers"}:
        raise _fail("MENU_REQUEST_INVALID", "发现阶段请求结构不完整，未启动 MSAA。")
    if (not _is_plain_int(request.get("schema_version")) or request["schema_version"] != 1
            or not _is_plain_int(request.get("root_hwnd")) or request["root_hwnd"] <= 0
            or not _is_plain_int(request.get("pid")) or request["pid"] <= 0
            or not isinstance(request.get("containers"), list)
            or not 1 <= len(request["containers"]) <= MAX_CONTAINERS):
        raise _fail("MENU_REQUEST_INVALID", "发现阶段请求字段无效，未启动 MSAA。")
    seen = set()
    for item in request["containers"]:
        if not isinstance(item, dict) or set(item) != {"hwnd", "class_name", "rect", "depth"}:
            raise _fail("MENU_REQUEST_INVALID", "发现阶段容器请求字段无效，未启动 MSAA。")
        rect = item.get("rect")
        if (not _is_plain_int(item.get("hwnd")) or item["hwnd"] <= 0
                or item["hwnd"] == request["root_hwnd"] or item["hwnd"] in seen
                or not isinstance(item.get("class_name"), str)
                or not item["class_name"].startswith(WINDOW_CLASS_PREFIX)
                or not isinstance(rect, list) or len(rect) != 4
                or any(not _is_plain_int(value) for value in rect)
                or not _is_plain_int(item.get("depth"))
                or not 1 <= item["depth"] <= MAX_DISCOVERY_DEPTH):
            raise _fail("MENU_REQUEST_INVALID", "发现阶段容器请求字段无效，未启动 MSAA。")
        seen.add(item["hwnd"])
    return request


def discover(request_path, log):
    """Create a minimal request after confirming the logged-in home window."""
    request_path = Path(request_path)
    log = Path(log)
    started = time.monotonic()

    settings = load_settings()
    if settings is None:
        raise _fail("MENU_SETTINGS_MISSING", "未找到 WDMS 本机设置，停止诊断。")
    executable = validate_executable(settings.wdms_exe)
    api = LoginNativeWindows()
    root = find_stage_window(api, "home", log)
    if not pid_matches(root["pid"], executable):
        raise _fail("MENU_PROCESS_MISMATCH", "主页窗口不属于本机配置的 WDMS 进程，停止诊断。")

    nodes = []
    mdi_found = False
    marker_found = False
    visited = 1  # The already-verified root is included in the node budget.
    pending = deque()

    def check_budget():
        if time.monotonic() - started >= MAX_DISCOVERY_SECONDS:
            raise _fail("MENU_DISCOVERY_TIME_LIMIT", "窗口发现超过 3 秒协作时限，停止诊断。")

    def walk_children(parent_hwnd, depth):
        nonlocal visited, mdi_found, marker_found
        check_budget()
        iterator = iter(api.child_handles(parent_hwnd))
        while True:
            check_budget()
            try:
                child_hwnd = next(iterator)
            except StopIteration:
                break
            check_budget()
            if not _is_plain_int(child_hwnd):
                raise _fail("MENU_WINDOW_METADATA_INVALID", "窗口句柄格式无效，停止诊断。")
            visited += 1
            if visited > MAX_DISCOVERY_NODES:
                raise _fail("MENU_NODE_LIMIT", "窗口节点超过 96 个，停止诊断且不截断结果。")
            metadata = api.metadata(child_hwnd)
            check_budget()
            if not isinstance(metadata, dict) or metadata.get("hwnd") != child_hwnd:
                raise _fail("MENU_WINDOW_METADATA_INVALID", "窗口元数据与句柄不一致，停止诊断。")
            if metadata.get("pid") != root["pid"]:
                continue
            class_name = metadata.get("class_name")
            rect = metadata.get("rect")
            if (not isinstance(class_name, str) or not isinstance(rect, (list, tuple))
                    or len(rect) != 4 or any(not _is_plain_int(value) for value in rect)):
                raise _fail("MENU_WINDOW_METADATA_INVALID", "窗口类别或矩形元数据无效，停止诊断。")
            node = {
                "hwnd": child_hwnd,
                "pid": root["pid"],
                "class_name": class_name,
                "visible": metadata.get("visible") is True,
                "rect": list(rect),
                "depth": depth,
            }
            nodes.append(node)

            if class_name == "MDICLIENT" or class_name.startswith("WindowsForms10.MDICLIENT."):
                mdi_found = True
            if node["visible"] and _is_static(class_name):
                label = _read_visible_static_label(api, node)
                if isinstance(label, str) and label.strip() == HOME_MARKER:
                    marker_found = True
            if class_name.startswith(WINDOW_CLASS_PREFIX) and depth < MAX_HOME_CONFIRMATION_DEPTH:
                pending.append((child_hwnd, depth + 1))

    # The root is enumerated exactly once. Only Window.8 containers recurse.
    walk_children(root["hwnd"], 1)
    while pending:
        check_budget()
        parent_hwnd, depth = pending.popleft()
        walk_children(parent_hwnd, depth)

    probe.record(
        log, "menu_home_confirmation", mdi_client_found=mdi_found,
        password_change_marker_found=marker_found,
        marker=HOME_MARKER if marker_found else None,
    )
    if not mdi_found or not marker_found:
        raise _fail("MENU_HOME_UNCONFIRMED", "未同时确认主页工作区和用户密码修改标记，停止诊断。")

    containers = select_candidates(nodes, root)
    request = {
        "schema_version": 1,
        "root_hwnd": int(root["hwnd"]),
        "pid": int(root["pid"]),
        "containers": containers,
    }
    _check_request_shape(request)
    _atomic_write_request(request_path, request)
    probe.record(log, "menu_discovery_complete", nodes_visited=visited,
                 containers=len(containers), request_written=True)
    return request


def validate_msaa_events(events, request):
    """Validate the bounded PowerShell result before reporting completion."""
    request = _check_request_shape(request)
    if not isinstance(events, list) or not events:
        raise _fail("MENU_RESULT_INCOMPLETE", "MSAA 结果为空或不完整。")
    forbidden = {
        "menu_msaa_limit", "menu_msaa_cancelled", "menu_msaa_timeout",
        "menu_msaa_error", "menu_msaa_partial", "incomplete_log_record",
        "menu_msaa_branch_pruned",
        "menu_msaa_container_pruned",
    }
    allowed_events = {
        "menu_msaa_container_started", "menu_msaa_container_complete",
        "menu_msaa_complete",
        "menu_msaa_node",
    }
    complete_events = []
    expected_hwnds = {item["hwnd"] for item in request["containers"]}
    started_hwnds = []
    completed_hwnds = []
    container_counts = {}
    active_hwnd = None
    traced_nodes = 0
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise _fail("MENU_RESULT_INVALID", "MSAA 结果记录格式无效。")
        kind = event["event"]
        if kind in forbidden:
            raise _fail("MENU_RESULT_INCOMPLETE", "MSAA 结果包含限制、取消、超时或不完整记录。")
        if kind not in allowed_events:
            raise _fail("MENU_RESULT_INVALID", "MSAA 结果包含未知记录，拒绝作为成功结果。")
        if kind == "menu_msaa_node":
            traced_nodes += 1
            if (set(event) != {"event", "node", "container_hwnd", "depth", "child_path", "path_kind", "node_kind", "role"}
                    or not _is_plain_int(event.get("container_hwnd"))
                    or event["container_hwnd"] != active_hwnd
                    or not _is_plain_int(event.get("node")) or event["node"] != traced_nodes
                    or not _is_plain_int(event.get("depth")) or not 0 <= event["depth"] <= 4
                    or not isinstance(event.get("child_path"), list)
                    or len(event["child_path"]) != event["depth"]
                    or any(not _is_plain_int(part) or part < 0 for part in event["child_path"])
                    or event.get("path_kind") not in {"child_id", "index_or_child_id"}
                    or event.get("node_kind") not in {"object", "element"}
                    or (event.get("role") is not None and not _is_plain_int(event["role"]))):
                raise _fail("MENU_RESULT_INVALID", "MSAA 节点结构记录无效。")
        elif kind == "menu_msaa_complete":
            complete_events.append(event)
        elif kind == "menu_msaa_container_started":
            hwnd = event.get("container_hwnd")
            if (not _is_plain_int(hwnd) or hwnd not in expected_hwnds
                    or hwnd in started_hwnds or active_hwnd is not None):
                raise _fail("MENU_RESULT_INVALID", "MSAA 容器开始记录与请求不匹配。")
            started_hwnds.append(hwnd)
            active_hwnd = hwnd
        else:
            hwnd = event.get("container_hwnd")
            if event.get("truncated", False) is not False:
                raise _fail("MENU_RESULT_INCOMPLETE", "MSAA 容器扫描被裁剪，不能判定完成。")
            if (not _is_plain_int(hwnd) or hwnd not in expected_hwnds
                    or hwnd in completed_hwnds or hwnd != active_hwnd):
                raise _fail("MENU_RESULT_INVALID", "MSAA 容器完成记录与请求不匹配。")
            if not _is_plain_int(event.get("matches_count")) or event["matches_count"] < 0:
                raise _fail("MENU_RESULT_INVALID", "MSAA 容器完成计数无效。")
            completed_hwnds.append(hwnd)
            container_counts[hwnd] = event["matches_count"]
            active_hwnd = None

    if len(complete_events) != 1 or events[-1].get("event") != "menu_msaa_complete":
        raise _fail("MENU_RESULT_INCOMPLETE", "MSAA 结果缺少唯一且位于末尾的完成标记。")
    if set(started_hwnds) != expected_hwnds or set(completed_hwnds) != expected_hwnds:
        raise _fail("MENU_RESULT_INCOMPLETE", "MSAA 容器处理数量与发现请求不一致。")

    complete = complete_events[0]
    if (not _is_plain_int(complete.get("schema_version")) or complete["schema_version"] != 1
            or not _is_plain_int(complete.get("nodes"))
            or not len(expected_hwnds) <= complete["nodes"] <= MAX_MSAA_NODES
            or not _is_plain_int(complete.get("elapsed_ms")) or complete["elapsed_ms"] < 0
            or complete.get("truncated") is not False
            or not _is_plain_int(complete.get("containers_completed"))
            or complete["containers_completed"] != len(expected_hwnds)
            or not isinstance(complete.get("matches"), list)):
        raise _fail("MENU_RESULT_INVALID", "MSAA 完成记录字段无效。")

    for match in complete["matches"]:
        if (not isinstance(match, dict)
                or set(match) != {"target", "container_hwnd", "child_path", "path_kind", "role", "state"}
                or match.get("target") not in ALLOWED_NAMES
                or not _is_plain_int(match.get("container_hwnd"))
                or match["container_hwnd"] not in expected_hwnds
                or not isinstance(match.get("child_path"), list)
                or len(match["child_path"]) > 5
                or any(not _is_plain_int(part) or part < 0 for part in match["child_path"])
                or match.get("path_kind") not in {"child_id", "index_or_child_id"}
                or not _is_plain_int(match.get("role"))
                or match["role"] not in ALLOWED_MSAA_ROLES
                or not _is_plain_int(match.get("state")) or match["state"] < 0):
            raise _fail("MENU_RESULT_INVALID", "MSAA 匹配项不符合白名单和路径约束。")
    if len(complete["matches"]) > complete["nodes"]:
        raise _fail("MENU_RESULT_INVALID", "MSAA 匹配数超过访问节点数。")
    if traced_nodes and traced_nodes != complete["nodes"]:
        raise _fail("MENU_RESULT_INVALID", "MSAA 节点结构记录数量与完成记录不一致。")
    for hwnd, count in container_counts.items():
        if count != sum(item["container_hwnd"] == hwnd for item in complete["matches"]):
            raise _fail("MENU_RESULT_INVALID", "MSAA 容器计数与实际匹配项不一致。")
    return complete


def _powershell_executable() -> Path:
    system_root = os.environ.get("SystemRoot")
    if not isinstance(system_root, str) or not system_root.strip():
        raise _fail("MENU_POWERSHELL_PATH_INVALID", "Windows SystemRoot 未设置，未启动 PowerShell。")
    return (Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _record_failure(log, stage, error=None, exit_code=None):
    data = {"stage": stage, "query_performed": False, "click_performed": False}
    if error is not None:
        data.update(probe.error_info(error))
    if exit_code is not None:
        data["exit_code"] = exit_code
    probe.record(log, "menu_diagnostic_failed", **data)


def _discover_entry(request_path, log):
    stage = "discover"
    try:
        discover(request_path, log)
        return 0
    except KeyboardInterrupt as error:
        if log is not None:
            try:
                _record_failure(log, stage, error=error)
            except Exception:
                # Failure logging is secondary; console output and exit code still report interruption.
                pass
        print(f"诊断已停止（阶段：{stage}）；没有执行登录、点击或查询。", flush=True)
        return 2
    except Exception as error:
        probe.record(log, "menu_discovery_error", **probe.error_info(error))
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="对已登录 WDMS 主页执行有界、只读的 MSAA 菜单诊断。")
    parser.add_argument("--out", type=Path, default=settings_path().parent / "menu_diagnostics")
    parser.add_argument("--cancel-file", type=Path, default=None)
    parser.add_argument("--_discover", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--log", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("此诊断仅支持 Windows；当前未连接 WDMS。", flush=True)
        return 2
    if args._discover:
        if args.request is None or args.log is None:
            print("发现阶段参数不完整。", flush=True)
            return 2
        return _discover_entry(args.request, args.log)

    log = None
    stage = "output_directory"
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        folder = args.out / f"menu_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex}"
        folder.mkdir()
        log = (folder / "diagnostic.jsonl").resolve()
        request_path = (folder / "request.json").resolve()
        result_path = (folder / "msaa.jsonl").resolve()
        probe.record(log, "menu_diagnostic_start", version=VERSION,
                     query_performed=False, click_performed=False)
        print(f"只读菜单诊断日志：{log}", flush=True)

        cancel_path = args.cancel_file.resolve() if args.cancel_file is not None else None
        cancelled = (lambda: cancel_path.exists()) if cancel_path is not None else None
        stage = "discover"
        command = [
            sys.executable, str(Path(__file__).resolve()), "--_discover",
            "--request", str(request_path), "--log", str(log),
        ]
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--menu-diagnostic'] + command[2:]
        code = probe.supervise(command, log, 10, cancelled=cancelled) if cancelled else probe.supervise(command, log, 10)
        if code != 0:
            _record_failure(log, stage, exit_code=code)
            print("窗口发现未完成；未启动 MSAA。", flush=True)
            return 2

        request = _check_request_shape(json.loads(request_path.read_text(encoding="utf-8")))
        stage = "powershell"
        powershell = _powershell_executable()
        script = Path(__file__).with_name("wdms_menu_msaa.ps1").resolve()
        command = [
            str(powershell), "-NoProfile", "-NonInteractive", "-STA",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-RequestFile", str(request_path), "-OutputFile", str(result_path),
        ]
        code = probe.supervise(command, log, 20, cancelled=cancelled) if cancelled else probe.supervise(command, log, 20)
        if code != 0:
            _record_failure(log, stage, exit_code=code)
            print("MSAA 诊断未完成；没有执行登录、点击或查询。", flush=True)
            return 2

        stage = "result_validation"
        events = probe.read_events(result_path)
        complete = validate_msaa_events(events, request)
        management_count = sum(1 for item in complete["matches"] if item["target"] == "在库管理")
        if management_count == 0:
            probe.record(log, "menu_target_not_exposed", target="在库管理", matches=0)
        elif management_count > 1:
            probe.record(log, "menu_target_ambiguous", target="在库管理", matches=management_count)
        probe.record(log, "menu_diagnostic_complete", version=VERSION,
                     read_only=True, query_performed=False, click_performed=False,
                     matches=len(complete["matches"]), nodes=complete["nodes"],
                     elapsed_ms=complete["elapsed_ms"])
        print(f"只读诊断完成；匹配数：{len(complete['matches'])}。日志目录：{folder}", flush=True)
        return 0
    except Exception as error:
        if log is not None:
            try:
                _record_failure(log, stage, error=error)
            except Exception:
                # Failure logging is secondary; console output and exit code still report the diagnostic failure.
                pass
        print(f"诊断未完成（阶段：{stage}）；没有执行登录、点击或查询。", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
