"""Supervised Win32 diagnostics; optional single query test. Never load UIA."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.windows_environment import inspect_environment, require_usable_environment
from errors import NativeTextReadError, WDMSError
from adapters.native_windows import (
    MouseInput, KeyboardInput, HardwareInput, InputUnion, NativeInput, CursorInfo,
    NativeWindows, NativeQuerySession, text_read_allowed, owner_combo_from_events, display_number_text,
    read_inventory_values, inventory_value_control, query_controls_from_events,
    check_native_controls,
)
from services.event_log import record, read_events, error_info, LOG_WRITE_RETRY_SECONDS
from services import process_supervisor
from core import native_collection


def supervise(command, log, timeout, cancelled=None, *, stderr_path=None):
    return process_supervisor.supervise(command, log, timeout, cancelled,
        stderr_path=stderr_path, emit=record, cwd=PROJECT_ROOT)


def wait_cursor_and_values(api, main, controls, owner, log, deadline, settle_seconds=5.0):
    session = NativeQuerySession(api, main, controls, log)
    return native_collection.wait_cursor_and_values(session, owner, deadline,
        emit=lambda event, **data: record(log, event, **data),
        clock=time.monotonic, sleep=time.sleep, settle_seconds=settle_seconds)


def test_current_owner_query(api, main, log, timeout, trigger="mouse", completion_signal="button", *, expected_owner=None, expected_index=None, check=lambda: None):
    session = NativeQuerySession(api, main, query_controls_from_events(read_events(log)), log, expected_index)
    return native_collection.query_current_owner(session, timeout, trigger, completion_signal,
        emit=lambda event, **data: record(log, event, **data), clock=time.monotonic,
        sleep=time.sleep, expected_owner=expected_owner, check=check)


def observe_query_signals(api, main, log, duration, trigger):
    session = NativeQuerySession(api, main, query_controls_from_events(read_events(log)), log)
    return native_collection.observe_query_signals(session, duration, trigger,
        emit=lambda event, **data: record(log, event, **data), clock=time.monotonic, sleep=time.sleep)


def child_main(args) -> int:
    log = Path(args.log)
    try:
        record(log, "before_environment_check")
        environment = inspect_environment()
        record(log, "environment", **environment)
        require_usable_environment(environment)
        record(log, "before_native_api_init")
        api = NativeWindows()
        matches = api.find_main(args.title, log)
        if not matches:
            raise WDMSError("WDMS_WINDOW_NOT_FOUND", "未找到标题完全匹配的可见 WDMS 主窗口。")
        if len(matches) != 1:
            raise WDMSError("WDMS_INSTANCE_AMBIGUOUS", "WDMS 主窗口不唯一，未继续检查。")
        main = matches[0]
        if main["minimized"]:
            raise WDMSError("WDMS_WINDOW_MINIMIZED", "请手动恢复 WDMS 窗口后检查。")
        api.health(main["hwnd"], log, "initial")
        if args.native_controls:
            check_native_controls(api, main, log, max_depth=getattr(args, "max_depth", 4), read_labels=getattr(args, "read_labels", False))
        if getattr(args, "list_owners", False):
            combo = owner_combo_from_events(read_events(log))
            if api.metadata(combo["hwnd"])["pid"] != main["pid"]:
                raise WDMSError("WDMS_INSTANCE_CHANGED", "货主下拉框所属进程变化。")
            api.health(main["hwnd"], log, "before_owner_list")
            api.read_owner_list(combo, log)
            api.health(main["hwnd"], log, "after_owner_list")
        if getattr(args, "observe_query", False):
            observe_query_signals(api, main, log, args.observe_seconds, args.query_trigger)
        if getattr(args, "test_query", False):
            test_current_owner_query(api, main, log, args.query_timeout, args.query_trigger, args.completion_signal)
        api.health(main["hwnd"], log, "final")
        record(log, "probe_success", mode="query_signal_observation" if getattr(args, "observe_query", False) else "single_query_observation" if getattr(args, "test_query", False) else "win32_label_text" if getattr(args, "read_labels", False) else "win32_metadata" if args.native_controls else "win32_window_only",
               uia_scan_performed=False, label_text_read_performed=getattr(args, "read_labels", False), inventory_collection_performed=False)
        return 0
    except Exception as error:
        record(log, "probe_error", **error_info(error))
        return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WDMS Win32 诊断：默认只读；--test-query 模拟一次输入触发查询。")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "test_logs")
    parser.add_argument("--title", default="医药企业仓储运输管理系统", help="主窗口完整标题")
    parser.add_argument("--native-controls", action="store_true", help="另外检查有限的原生 HWND 元数据，仍不读 UIA 或控件文本")
    parser.add_argument("--max-depth", type=int, choices=range(1, 9), default=4, help="原生 HWND 检查深度（1–8，默认 4）")
    parser.add_argument("--read-labels", action="store_true", help="只读可见 WinForms 标签、按钮和下拉框文字；不读输入框，不点击")
    parser.add_argument("--list-owners", action="store_true", help="只读货主下拉框的全部名称，不切换货主、不触发查询")
    query_modes = parser.add_mutually_exclusive_group()
    query_modes.add_argument("--observe-query", action="store_true", help="触发一次查询，记录光标、按钮及读数变化；只诊断，不判断采集成功")
    parser.add_argument("--observe-seconds", type=int, choices=range(5, 46), default=15)
    query_modes.add_argument("--test-query", action="store_true", help="只对当前选中货主模拟一次输入触发查询，观察按钮和读数；不生成正式报表")
    parser.add_argument("--completion-signal", choices=("button", "cursor"), default="button", help="单货主测试完成信号：cursor 要求等待光标结束且读数稳定 5 秒")
    parser.add_argument("--query-trigger", choices=("mouse", "alt-f"), default="mouse", help="查询触发方式：mouse 点击按钮；alt-f 发送快捷键")
    parser.add_argument("--query-timeout", type=int, choices=range(5, 61), default=45, help="一次查询的观察时限（5–60 秒）")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.list_owners or args.test_query or args.observe_query:
        args.read_labels = True
    if args.read_labels:
        args.native_controls = True
    if args._child:
        return child_main(args)
    args.out.mkdir(parents=True, exist_ok=True)
    log = (args.out / f"onsite_win32_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl").resolve()
    record(log, "probe_start", version="20261013-win32-2.11", mode="query_signal_observation" if args.observe_query else "single_query_observation" if args.test_query else "win32_label_text" if args.read_labels else "win32_metadata" if args.native_controls else "win32_window_only", max_depth=args.max_depth)
    command = [sys.executable, str(Path(__file__).resolve()), "--_child", "--log", str(log), "--title", args.title, "--max-depth", str(args.max_depth)]
    if args.native_controls:
        command.append("--native-controls")
    if args.read_labels:
        command.append("--read-labels")
    if args.list_owners:
        command.append("--list-owners")
    if args.test_query:
        command.extend(["--test-query", "--query-timeout", str(args.query_timeout), "--query-trigger", args.query_trigger, "--completion-signal", args.completion_signal])
    if args.observe_query:
        command.extend(["--observe-query", "--observe-seconds", str(args.observe_seconds), "--query-trigger", args.query_trigger])
    print("Win32 窗口诊断 v2.11：不加载 UIA。", flush=True)
    if args.test_query or args.observe_query:
        print(f"本次将激活 WDMS 并{'发送 Alt+F' if args.query_trigger == 'alt-f' else '移动鼠标点击查询'}一次。测试期间请勿操作鼠标键盘；不切换货主、不自动重试、不生成正式报表。", flush=True)
    print("本轮检查：有限原生 HWND 元数据。" if args.native_controls else "本轮检查：仅顶层窗口与响应状态，不枚举子控件。", flush=True)
    print(f"实时证据：{log}", flush=True)
    code = supervise(command, log, timeout=args.observe_seconds + 15.0 if args.observe_query else args.query_timeout + 15.0 if args.test_query else 10.0)
    events = read_events(log)
    successful = code == 0 and any(item["event"] == "probe_success" for item in events)
    if not successful:
        child_exit_code = code
        code = 2
        if not any(item["event"] in ("probe_error", "probe_timeout", "probe_interrupted", "probe_start_failed") for item in events):
            record(log, "probe_child_failed", error_code="PROBE_CHILD_FAILED", child_exit_code=child_exit_code)
        for item in events:
            if item["event"].startswith("before_"):
                last_step = item["event"]
        print(f"检查失败；最后开始的步骤：{locals().get('last_step', '子进程启动')}。")
        for item in events:
            if item.get("error_code"):
                print(json.dumps(item, ensure_ascii=False))
    else:
        for item in events:
            if item["event"] == "after_window_enumeration":
                print(json.dumps(item["matches"], ensure_ascii=False))
            elif item["event"] == "native_text":
                print(f"hwnd={item['hwnd']} rect={item['rect']} text={item['text']!r}")
            elif item["event"] == "owner_list_complete":
                print(json.dumps(item, ensure_ascii=False))
            elif item["event"] in ("single_query_observation_complete", "query_signal_state", "query_signal_values_changed", "query_signal_observation_complete"):
                print(json.dumps(item, ensure_ascii=False))
        if args.observe_query:
            print("信号观察已结束；尚未证明查询完成，不代表库存采集成功。")
        print("窗口检查通过。仅代表本次窗口探活成功，尚未验证控件或库存采集。")
    record(log, "probe_finished", exit_code=code)
    print(f"JSONL 证据：{log}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())


# Compatibility exports used by staged diagnostics and field scripts.
__all__ = ['CursorInfo', 'HardwareInput', 'InputUnion', 'KeyboardInput', 'LOG_WRITE_RETRY_SECONDS', 'MouseInput', 'NativeInput', 'NativeTextReadError', 'child_main', 'ctypes', 'display_number_text', 'inventory_value_control', 'main', 'observe_query_signals', 'read_inventory_values', 'subprocess', 'supervise', 'test_current_owner_query', 'text_read_allowed', 'wait_cursor_and_values', 'wintypes']
