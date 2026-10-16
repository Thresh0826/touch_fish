"""Bounded, read-only owner-list capture after navigation; no selections/queries."""
from __future__ import annotations

import argparse
import math
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from desktop_demo import login, navigation
from desktop_demo.settings import load_settings, settings_path, validate_executable
from errors import WDMSError
from scripts import check_wdms_window as probe
from scripts.check_wdms_startup import find_stage_window
from services import owner_catalog


def child(args):
    log = Path(args.log)
    try:
        config = load_settings()
        if config is None:
            raise WDMSError('SETTINGS_MISSING', '请先保存本机配置。')
        exe = validate_executable(config.wdms_exe)
        probe.require_usable_environment(probe.inspect_environment())
        api = navigation.NavigationNativeWindows()
        root = find_stage_window(api, 'home', log)
        if not login.pid_matches(root['pid'], exe):
            raise WDMSError('OWNER_PROCESS_MISMATCH', '名单窗口不属于配置的WDMS，未读取。')
        api.health(root['hwnd'], log, 'before_owner_list')
        number = [0]
        def scan(stage):
            number[0] += 1
            path = log.with_name(f'{log.stem}_scan_{number[0]:03d}.jsonl')
            probe.record(log, 'owner_page_scan', stage=stage, scan_file=str(path))
            try:
                probe.check_native_controls(api, root, path, max_depth=8, budget=25, read_labels=True)
                return navigation.supplement_navigation_captions(api, root, probe.read_events(path), path)
            except Exception as error:
                probe.record(path, 'native_scan_failed', **probe.error_info(error))
                raise
        events = navigation.scan_page_state(api, root, log, scan, 'owner_page',
                                           time.monotonic()+25, getattr(args, 'cancel_file', None))
        if not navigation.confirm_inventory_page(api, root, events, log, already_open=True):
            raise WDMSError('OWNER_PAGE_UNCONFIRMED', '未确认实际检索页，未读取货主名单。')
        control = navigation.inventory_page_controls(events)['owner']
        def check_control():
            live = api.metadata(control['hwnd'])
            if (live['pid'] != root['pid'] or live['class_name'] != control['class_name']
                    or not api.user32.IsWindowEnabled(control['hwnd'])
                    or navigation.inspect_geometry(api, root, {**control, **live})['visible_rect'] is None):
                raise WDMSError('OWNER_CONTROL_CHANGED', '货主下拉身份、状态或客户区变化，未继续读取。')
            return {**control, **live}
        first = owner_catalog.validated_names(api.read_owner_list(check_control(), log))
        time.sleep(0.2)
        second = owner_catalog.validated_names(api.read_owner_list(check_control(), log))
        if first != second:
            raise WDMSError('OWNER_LIST_UNSTABLE', '两次读取的货主名单不一致，停止且未查询。')
        live = api.metadata(root['hwnd'])
        if (live['pid'] != root['pid'] or live['class_name'] != root['class_name']
                or not live['visible'] or live['minimized'] or not api.user32.IsWindowEnabled(root['hwnd'])
                or not login.pid_matches(root['pid'], exe)):
            raise WDMSError('OWNER_SESSION_CHANGED', '读取后WDMS主窗口身份或状态变化，未保存名单。')
        owner_catalog.write_snapshot(args.snapshot, second, config, root)
        probe.record(log, 'owner_list_capture_complete', count=len(second),
                     fingerprint=owner_catalog.fingerprint(second), pid=root['pid'],
                     main_hwnd=root['hwnd'], actions_performed=False, query_performed=False)
        return 0
    except Exception as error:
        probe.record(log, 'probe_error', **probe.error_info(error))
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读货主下拉列表，保存本轮名单；不点击或查询。')
    parser.add_argument('--out', type=Path, default=settings_path().parent/'owner_logs')
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--execution-deadline', type=float, default=None)
    parser.add_argument('--cancel-file', type=Path, default=None)
    parser.add_argument('--_child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--log', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.execution_deadline is not None and not math.isfinite(args.execution_deadline):
        parser.error('读取期限必须是有限的monotonic绝对期限。')
    if args._child:
        return child(args)
    args.out.mkdir(parents=True, exist_ok=True)
    log = args.out / f'owner_list_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl'
    probe.record(log, 'owner_list_capture_started', revision=owner_catalog.SNAPSHOT_REVISION)
    entry = Path(__file__).resolve().parents[1]/'scripts'/'WDMS_Demo.py'
    command = ([sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(entry)])
    command += ['--native-owners', '--_child', '--log', str(log.resolve()), '--snapshot', str(args.snapshot.resolve())]
    if args.cancel_file is not None:
        command += ['--cancel-file', str(args.cancel_file.resolve())]
    timeout = 75
    if args.execution_deadline is not None:
        timeout = min(timeout, args.execution_deadline-time.monotonic())
    cancelled = (lambda:args.cancel_file.resolve().exists()) if args.cancel_file is not None else None
    code = probe.supervise(command, log, timeout, cancelled=cancelled)
    if cancelled is not None and cancelled():
        probe.record(log,'probe_interrupted',error_code='TASK_CANCELLED')
        return 2
    if code != 0:
        return 2
    try:
        document, names, unused = owner_catalog.read_plan(args.snapshot)
        completed = [event for event in probe.read_events(log) if event.get('event')=='owner_list_capture_complete']
        if (len(completed)!=1 or completed[0].get('count')!=len(names)
                or completed[0].get('fingerprint')!=document.get('fingerprint')
                or document.get('revision')!=owner_catalog.SNAPSHOT_REVISION
                or completed[0].get('pid')!=document['identity']['pid']
                or completed[0].get('main_hwnd')!=document['identity']['main_hwnd']):
            raise WDMSError('OWNER_CAPTURE_UNCONFIRMED','读取阶段没有完整的本轮名单确认，未开始查询。')
    except Exception as error:
        probe.record(log,'probe_error',**probe.error_info(error))
        return 2
    return 0
