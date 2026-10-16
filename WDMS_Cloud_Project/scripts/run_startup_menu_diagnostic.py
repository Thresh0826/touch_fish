"""Start/login through the existing verified login flow, then inspect menus."""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_demo import login
from desktop_demo.settings import settings_path
from errors import WDMSError
from scripts import check_wdms_window as probe
from scripts import diagnose_wdms_menu as menu
from services.task_lock import TaskLock


def run_stages(folder, cancel_file=None):
    folder = Path(folder)
    log = folder / "startup_menu.jsonl"
    login_dir = folder / "login"
    menu_dir = folder / "menu"

    def check_cancelled():
        if cancel_file is not None and Path(cancel_file).exists():
            raise WDMSError("TASK_CANCELLED", "流程已取消，未继续后续步骤。")

    def confirmed(directory, marker, **required):
        return any(event.get("event") == marker
                   and all(event.get(key) is value for key, value in required.items())
                   for path in directory.rglob("*.jsonl")
                   for event in probe.read_events(path))

    check_cancelled()
    login_args = ["--out", str(login_dir)]
    if cancel_file is not None:
        login_args += ["--cancel-file", str(cancel_file)]
    probe.record(log, "startup_menu_login_started")
    if login.main(login_args) != 0:
        raise WDMSError("STARTUP_MENU_LOGIN_FAILED", "自动启动或登录未完成，未进行菜单诊断。")
    if not confirmed(login_dir, "automatic_login_complete", login_success_verified=True):
        raise WDMSError("STARTUP_MENU_LOGIN_UNCONFIRMED", "登录缺少主页成功确认，未进行菜单诊断。")
    probe.record(log, "startup_menu_login_complete")
    check_cancelled()
    menu_args = ["--out", str(menu_dir)]
    if cancel_file is not None:
        menu_args += ["--cancel-file", str(cancel_file)]
    probe.record(log, "startup_menu_inspection_started")
    if menu.main(menu_args) != 0:
        raise WDMSError("STARTUP_MENU_INSPECTION_FAILED", "已完成登录，但菜单诊断未完成。")
    if not confirmed(menu_dir, "menu_diagnostic_complete", read_only=True,
                     click_performed=False, query_performed=False):
        raise WDMSError("STARTUP_MENU_INSPECTION_UNCONFIRMED", "菜单诊断缺少完整成功确认。")
    probe.record(log, "startup_menu_complete", login_verified=True,
                 menu_navigation_performed=False, query_performed=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="自动启动 WDMS、登录并确认主页，然后执行菜单只读诊断。")
    parser.add_argument("--out", type=Path, default=settings_path().parent / "startup_menu_diagnostics")
    parser.add_argument("--cancel-file", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("此流程仅支持 Windows；当前未连接 WDMS。", flush=True)
        return 2
    folder = args.out.resolve() / f"startup_menu_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    log = folder / "startup_menu.jsonl"
    cancel_file = args.cancel_file.resolve() if args.cancel_file else folder / "cancel.request"
    print(f"自动启动、登录和菜单诊断目录：{folder}", flush=True)
    try:
        with TaskLock(settings_path().parent / "workflow.lock"):
            run_stages(folder, cancel_file)
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            error = WDMSError("TASK_CANCELLED", "流程收到中断，已停止。")
        probe.record(log, "startup_menu_failed", **probe.error_info(error))
        print("流程未完成，请查看本轮日志；不会重试登录或继续查询。", flush=True)
        return 2
    print("自动登录及只读菜单诊断完成；尚未执行菜单导航或库存查询。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
