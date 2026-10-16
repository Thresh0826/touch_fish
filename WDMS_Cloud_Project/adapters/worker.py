"""Isolated Windows UIA/Win32 worker. Never close or restart the WDMS process."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import validate_real_config
from errors import WDMSError
from adapters.windows_environment import inspect_environment, require_usable_environment


class WindowsWorker:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.real = config["real"]
        self.main_cfg = self.real["main_window"]
        self.login_cfg = self.real["login"]
        self.control_cfg = self.real["controls"]
        self.evidence_cfg = self.real["query_evidence"]
        self.pid: Optional[int] = None
        self.hwnd: Optional[int] = None
        self.main = None
        self.controls: Dict[str, Any] = {}
        self.selected_owner: Optional[str] = None
        self.pending: Optional[Dict[str, Any]] = None
        self._closed = False
        self._windows_loaded = False
        self._scan_log: Optional[Path] = None

    def dispatch(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        handlers = {
            "connect": self.connect,
            "login": self.login,
            "navigate": self.navigate,
            "list_owners": self.list_owners,
            "select_owner": self.select_owner,
            "start_query": self.start_query,
            "read_result": self.read_result,
            "close": self.close,
        }
        handler = handlers.get(action)
        if not handler:
            raise WDMSError("WORKER_ACTION_INVALID", "自动化动作不受支持。")
        return handler(payload)

    def _ensure_windows(self) -> None:
        if sys.platform != "win32":
            raise WDMSError("REAL_MODE_WINDOWS_ONLY", "WDMS 自动化只能在 Windows 上执行。")
        if self._windows_loaded:
            return
        try:
            import ctypes
            from ctypes import wintypes
            import psutil
            import win32api
            import win32con
            import win32gui
            import win32process
            from pywinauto import Application, Desktop
        except ImportError as error:
            raise WDMSError("WINDOWS_DEPENDENCY_MISSING", "Windows 自动化依赖未安装。") from error
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.psutil = psutil
        self.win32api = win32api
        self.win32con = win32con
        self.win32gui = win32gui
        self.win32process = win32process
        self.Application = Application
        self.Desktop = Desktop
        # ctypes defaults undeclared function arguments and return values to
        # 32-bit ``int``.  Explicit pointer-sized signatures are required for
        # HWND/LRESULT on 64-bit Windows.
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._is_hung_app_window = self.user32.IsHungAppWindow
        self._is_hung_app_window.argtypes = [wintypes.HWND]
        self._is_hung_app_window.restype = wintypes.BOOL
        self._send_message_timeout = self.user32.SendMessageTimeoutW
        self._send_message_timeout.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            getattr(wintypes, "WPARAM", ctypes.c_size_t),
            getattr(wintypes, "LPARAM", ctypes.c_ssize_t),
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._send_message_timeout.restype = ctypes.c_ssize_t
        self._windows_loaded = True

    def connect(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        require_usable_environment(inspect_environment())
        validate_real_config(self.config)
        self._validate_gate()
        self._ensure_windows()
        trace_dir = Path.cwd() / "state" / "real"
        try:
            trace_dir.mkdir(parents=True, exist_ok=True)
            self._scan_log = trace_dir / f"worker_scan_{uuid.uuid4().hex}.jsonl"
            self._trace_scan("connect.begin")
        except OSError as error:
            raise WDMSError("SCAN_TRACE_WRITE_FAILED", "无法建立控件诊断记录；尚未扫描 UIA 控件树。") from error
        path = Path(str(self.main_cfg.get("executable_path", ""))).expanduser()
        if not path.is_absolute():
            raise WDMSError("REAL_CONFIG_INCOMPLETE", "WDMS 可执行路径必须是本机绝对路径。")
        matches = self._find_target_process_windows()
        if not matches:
            if not self.real.get("launch_if_missing", True):
                raise WDMSError("WDMS_NOT_RUNNING", "没有找到符合配置的 WDMS 主窗口。")
            if not path.is_file():
                raise WDMSError("WDMS_EXECUTABLE_MISSING", "配置的 WDMS 可执行文件不存在。")
            try:
                import subprocess

                subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True)
            except OSError as error:
                raise WDMSError("WDMS_START_FAILED", "无法启动配置指定的 WDMS 客户端。") from error
            deadline = time.monotonic() + float(self.real.get("startup_timeout_seconds", 30))
            while time.monotonic() < deadline:
                time.sleep(0.35)
                matches = self._find_target_process_windows()
                if matches:
                    break
            if not matches:
                raise WDMSError("WDMS_WINDOW_NOT_FOUND", "WDMS 启动后未能识别唯一主窗口。")
        if len(matches) != 1:
            raise WDMSError("WDMS_INSTANCE_AMBIGUOUS", "符合配置的 WDMS 实例不唯一。")
        self.hwnd, self.pid = matches[0]
        self._preflight(allow_login_window=True)
        try:
            # Process identity and executable path were already validated above.
            # HWND connection avoids pywinauto's redundant OpenProcess request.
            self._trace_scan("connect.before_uia_connect")
            self.app = self.Application(backend="uia").connect(handle=self.hwnd)
            self._trace_scan("connect.before_wrapper_object")
            self.main = self.app.window(handle=self.hwnd).wrapper_object()
            self._trace_scan("connect.complete")
        except Exception as error:
            raise self._connection_error(error) from error
        return {"connected": True}

    def login(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_connected()
        self._preflight(allow_login_window=True)
        login_hwnds = self._login_windows()
        if login_hwnds and self.login_cfg.get("mode", "automatic") == "existing_session":
            raise WDMSError("LOGIN_REQUIRED", "当前配置只使用已登录会话；请先手动登录 WDMS，再重新运行。")
        if not login_hwnds:
            state = self._find_unique(self.main, self.login_cfg["session_marker_id"], required=False)
            if state is not None and self._read_source(state, self.login_cfg.get("session_marker_text_source", "window_text")) == self.login_cfg["session_marker_value"]:
                return {"logged_in": True, "branch": "existing_session"}
            raise WDMSError("LOGIN_STATE_UNCONFIRMED", "未找到登录窗口，也无法确认已登录会话。")
        if len(login_hwnds) != 1:
            raise WDMSError("LOGIN_WINDOW_AMBIGUOUS", "登录窗口不唯一，已停止操作。")
        login_window = self._uia_window(login_hwnds[0])
        login_controls = self._find_controls(
            login_window,
            [self.login_cfg["username_control_id"], self.login_cfg["password_control_id"], self.login_cfg["submit_button_id"], self.login_cfg["failure_marker_id"]],
        )
        username_control = login_controls[self.login_cfg["username_control_id"]]
        password_control = login_controls[self.login_cfg["password_control_id"]]
        submit_button = login_controls[self.login_cfg["submit_button_id"]]
        failure_control = login_controls[self.login_cfg["failure_marker_id"]]
        username = str(self.login_cfg.get("username", ""))
        if not username:
            raise WDMSError("LOGIN_CREDENTIAL_MISSING", "Real 模式缺少配置用户名。")
        password = self._read_credential()
        self._preflight(allow_login_window=True)
        try:
            username_control.set_edit_text(username)
            password_control.set_edit_text(password)
            submit_button.invoke()
        except Exception as error:
            password = ""
            raise WDMSError("LOGIN_ACTION_FAILED", "登录控件操作失败。") from error
        password = ""

        deadline = time.monotonic() + float(self.login_cfg.get("timeout_seconds", 30))
        while time.monotonic() < deadline:
            self._preflight(allow_login_window=True)
            remaining_login = self._login_windows()
            if login_hwnds[0] in remaining_login:
                if self._read_source(failure_control, self.login_cfg.get("failure_marker_text_source", "window_text")) == self.login_cfg["failure_marker_value"]:
                    raise WDMSError("LOGIN_REJECTED", "WDMS 显示登录失败标记。")
            scope = self.login_cfg.get("success_marker_scope", "main")
            root = self.main if scope == "main" else (login_window if login_hwnds[0] in remaining_login else None)
            success_control = self._find_unique(root, self.login_cfg["success_marker_id"], required=False) if root is not None else None
            if success_control is not None and self._read_source(success_control, self.login_cfg.get("success_marker_text_source", "window_text")) == self.login_cfg["success_marker_value"]:
                return {"logged_in": True, "branch": "credentials"}
            time.sleep(0.75)
        raise WDMSError("LOGIN_TIMEOUT", "登录成功标记未在配置时限内出现。")

    def navigate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_connected()
        for step in self.real.get("navigation", {}).get("steps", []):
            self._preflight()
            action = step.get("action", "invoke")
            control = self._find_unique(self.main, step["automation_id"])
            try:
                if action == "invoke":
                    control.invoke()
                elif action == "select":
                    control.select(step.get("selection", ""))
                else:
                    raise WDMSError("NAVIGATION_CONFIG_INVALID", "导航动作类型不受支持。")
            except WDMSError:
                raise
            except Exception as error:
                raise WDMSError("NAVIGATION_ACTION_FAILED", "WDMS 导航控件操作失败。") from error
            marker = self._find_unique(self.main, step["page_marker_id"], required=False)
            if marker is None:
                raise WDMSError("NAVIGATION_PAGE_UNCONFIRMED", "导航后未能定位配置的目标页面标记。")
            deadline = time.monotonic() + float(step.get("timeout_seconds", 12))
            while time.monotonic() < deadline:
                self._preflight()
                if self._read_source(marker, step.get("page_marker_text_source", "window_text")) == step["page_marker_value"]:
                    break
                time.sleep(0.75)
            if self._read_source(marker, step.get("page_marker_text_source", "window_text")) != step["page_marker_value"]:
                raise WDMSError("NAVIGATION_PAGE_UNCONFIRMED", "WDMS 导航后未能确认目标页面。")
        self._cache_inventory_controls()
        return {"navigated": True}

    def list_owners(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_inventory_controls()
        self._preflight()
        if self.real.get("owner_list_strategy") == "complete_list":
            try:
                # UIA ComboBoxWrapper exposes texts(); the inventory service
                # subsequently validates the returned owner set exactly and
                # rejects duplicates or incomplete lists.
                names = self.controls[self.control_cfg["owner_combo_id"]].texts()
            except Exception as error:
                raise WDMSError("OWNER_LIST_READ_FAILED", "WDMS 完整货主清单无法读取。") from error
            if not isinstance(names, (list, tuple)):
                raise WDMSError("OWNER_LIST_READ_FAILED", "WDMS 完整货主清单格式无效。")
            owners = [{"id": str(name), "name": str(name)} for name in names]
        elif self.real.get("owner_list_strategy") == "verified_name_select" and self.real.get("verified_name_select_confirmed"):
            owners = [{"id": str(row.get("id") or row["name"]), "name": str(row["name"])} for row in self.config.get("owners", [])]
        else:
            raise WDMSError("OWNER_LIST_UNVERIFIED", "货主清单读取策略未通过现场确认。")
        return {"owners": owners}

    def select_owner(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_inventory_controls()
        name = str(payload.get("owner_name", ""))
        if not name:
            raise WDMSError("OWNER_SELECTION_FAILED", "目标货主名称为空。")
        self._preflight()
        combo = self.controls[self.control_cfg["owner_combo_id"]]
        try:
            combo.select(name)
            selected = combo.selected_text()
        except Exception as error:
            raise WDMSError("OWNER_SELECTION_FAILED", "无法通过精确货主名称选择 WDMS 货主。") from error
        if selected != name:
            raise WDMSError("OWNER_SELECTION_FAILED", "WDMS 货主回读名称与目标名称不一致。")
        self.selected_owner = name
        return {"selected": True}

    def start_query(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_inventory_controls()
        owner_name = str(payload.get("owner_name", ""))
        if owner_name != self.selected_owner:
            raise WDMSError("OWNER_SELECTION_FAILED", "查询前当前货主与目标不一致。")
        self._preflight()
        combo = self.controls[self.control_cfg["owner_combo_id"]]
        if combo.selected_text() != owner_name:
            raise WDMSError("OWNER_SELECTION_FAILED", "查询前货主回读校验失败。")
        evidence = self.evidence_cfg
        before: Dict[str, Any] = {"strategy": evidence["strategy"]}
        if evidence["strategy"] == "version":
            before["version"] = self._signal("version")
        else:
            busy = self._signal("busy")
            if busy != str(evidence["idle_value"]):
                raise WDMSError("QUERY_ALREADY_RUNNING", "查询前 WDMS 未处于已确认的空闲状态。")
            before["busy_before"] = busy
        request_id = str(uuid.uuid4())
        self.pending = {"request_id": request_id, "owner_name": owner_name, "owner_id": payload.get("owner_id"), "evidence_before": before, "started": time.monotonic()}
        self._preflight()
        try:
            self.controls[self.control_cfg["search_button_id"]].invoke()
        except Exception as error:
            self.pending = None
            raise WDMSError("QUERY_TRIGGER_FAILED", "WDMS 查询按钮调用失败。") from error
        return {"request_id": request_id, "evidence": before}

    def read_result(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_inventory_controls()
        pending = self.pending
        request_id = str(payload.get("request_id", ""))
        if not pending or request_id != pending["request_id"] or payload.get("owner_name") != pending["owner_name"]:
            raise WDMSError("QUERY_TICKET_INVALID", "本次查询凭据无效或货主不匹配。")
        timeout = min(float(self.config.get("timing", {}).get("query_timeout_seconds", 15)), float(payload.get("timeout_seconds", 15)))
        deadline = time.monotonic() + timeout
        evidence = self.evidence_cfg
        saw_busy = False
        completion_value = None
        while time.monotonic() < deadline:
            self._preflight()
            if self.controls[self.control_cfg["owner_combo_id"]].selected_text() != pending["owner_name"]:
                raise WDMSError("QUERY_OWNER_MISMATCH", "查询期间当前货主发生变化。")
            result_owner = self._signal("result_owner")
            busy = None
            if evidence["strategy"] == "transition":
                # The old owner may remain visible while the new query is busy.
                # Record the busy transition independently of result ownership.
                busy = self._signal("busy")
                if busy == str(evidence["busy_value"]):
                    saw_busy = True
            if result_owner == pending["owner_name"]:
                if evidence["strategy"] == "version":
                    version = self._signal("version")
                    complete = self._signal("complete")
                    if version != pending["evidence_before"]["version"] and complete == str(evidence["complete_value"]):
                        completion_value = complete
                        break
                else:
                    if busy == str(evidence["idle_value"]) and saw_busy:
                        completion_value = busy
                        break
            time.sleep(0.75)
        if completion_value is None:
            self.pending = None
            raise WDMSError("QUERY_TIMEOUT", "未在配置时限内观察到已验证的本次查询完成及货主归属信号。")

        self._preflight()
        owner_before_read = self._signal("result_owner")
        if owner_before_read != pending["owner_name"]:
            self.pending = None
            raise WDMSError("QUERY_OWNER_MISMATCH", "读取库存前结果货主与目标不一致。")
        version_before_read = self._signal("version") if evidence["strategy"] == "version" else None
        try:
            whole = self._read_source(self.controls[self.control_cfg["whole_count_id"]], self.control_cfg.get("whole_count_text_source", "window_text"))
            loose = self._read_source(self.controls[self.control_cfg["loose_count_id"]], self.control_cfg.get("loose_count_text_source", "window_text"))
        except Exception as error:
            self.pending = None
            raise WDMSError("RESULT_READ_FAILED", "WDMS 整件数或散件数控件读取失败。") from error
        self._preflight()
        owner_after_read = self._signal("result_owner")
        if owner_after_read != owner_before_read:
            self.pending = None
            raise WDMSError("QUERY_OWNER_MISMATCH", "读取期间 WDMS 结果货主发生变化。")
        version_after_read = self._signal("version") if evidence["strategy"] == "version" else None
        if evidence["strategy"] == "version" and version_after_read != version_before_read:
            self.pending = None
            raise WDMSError("QUERY_EVIDENCE_CHANGED", "读取期间 WDMS 结果版本发生变化。")
        if evidence["strategy"] == "transition" and self._signal("busy") != str(evidence["idle_value"]):
            self.pending = None
            raise WDMSError("QUERY_EVIDENCE_CHANGED", "读取期间 WDMS 查询状态发生变化。")
        if self.controls[self.control_cfg["owner_combo_id"]].selected_text() != pending["owner_name"]:
            self.pending = None
            raise WDMSError("QUERY_OWNER_MISMATCH", "读取期间当前货主选择发生变化。")
        result = {
            "request_id": pending["request_id"],
            "owner_id": pending["owner_id"],
            "owner_name": pending["owner_name"],
            "whole_count": whole,
            "loose_count": loose,
            "evidence": {
                "strategy": evidence["strategy"],
                "validation_record": evidence["validation_record"],
                "version_before": pending["evidence_before"].get("version"),
                "version_after": version_after_read,
                "completion": completion_value,
                "owner_match": True,
            },
        }
        self.pending = None
        return result

    def close(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.pending = None
        self.controls.clear()
        self.main = None
        self._closed = True
        return {"closed": True}

    def _validate_gate(self) -> None:
        evidence = self.evidence_cfg
        if not evidence.get("confirmed") or evidence.get("strategy") not in ("version", "transition"):
            raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "查询完成与结果归属信号尚未现场验证，Real 模式已阻断。")
        if not evidence.get("validation_record") or not evidence.get("verified_date") or not evidence.get("client_version"):
            raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "查询证据验证记录、日期或客户端版本缺失。")

    def _find_target_process_windows(self) -> List[Tuple[int, int]]:
        self._ensure_windows()
        expected_name = str(self.main_cfg.get("process_name", ""))
        expected_path = os.path.normcase(os.path.abspath(str(self.main_cfg.get("executable_path", ""))))
        processes = []
        denied_target = False
        for process in self.psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = process.info.get("name") or ""
                executable = process.info.get("exe") or ""
            except (self.psutil.AccessDenied, self.psutil.NoSuchProcess):
                continue
            if name.lower() == expected_name.lower():
                if not executable:
                    denied_target = True
                    continue
                if os.path.normcase(os.path.abspath(executable)) != expected_path:
                    continue
                processes.append(int(process.info["pid"]))
        if len(processes) > 1:
            raise WDMSError("WDMS_INSTANCE_AMBIGUOUS", "配置路径对应的 WDMS 进程不唯一。")
        if not processes:
            if denied_target:
                raise WDMSError("WDMS_PROCESS_IDENTITY_UNREADABLE", "发现同名 WDMS 进程，但无法读取其可执行路径。请检查受限令牌、完整性和会话权限；未启动第二个客户端。")
            return []
        pid = processes[0]
        pattern = re.compile(str(self.main_cfg.get("title_regex", "")))
        expected_class = str(self.main_cfg.get("class_name", ""))
        configured_pid = self.main_cfg.get("pid")
        if configured_pid is not None and int(configured_pid) != pid:
            raise WDMSError("WDMS_INSTANCE_MISMATCH", "WDMS 进程 PID 与配置不一致。")
        candidates: List[Tuple[int, int]] = []

        def enum_callback(hwnd, _):
            try:
                if not self.win32gui.IsWindowVisible(hwnd):
                    return True
                _, window_pid = self.win32process.GetWindowThreadProcessId(hwnd)
                if window_pid != pid:
                    return True
                title = self.win32gui.GetWindowText(hwnd)
                class_name = self.win32gui.GetClassName(hwnd)
                if class_name == expected_class and pattern.fullmatch(title) and not self.win32gui.GetWindow(hwnd, 4):
                    candidates.append((int(hwnd), int(pid)))
            except Exception:
                return True
            return True

        self.win32gui.EnumWindows(enum_callback, None)
        if len(candidates) > 1:
            raise WDMSError("WDMS_INSTANCE_AMBIGUOUS", "符合窗口类名及标题的 WDMS 主窗口不唯一。")
        if not candidates:
            raise WDMSError("WDMS_WINDOW_SELECTOR_MISMATCH", "WDMS 进程存在，但主窗口标题或类名与配置不符。")
        return candidates

    def _login_windows(self) -> List[int]:
        self._ensure_windows()
        pattern_text = str(self.login_cfg.get("window_title_regex", ""))
        if not pattern_text:
            return []
        pattern = re.compile(pattern_text)
        matches = []

        def enum_callback(hwnd, _):
            try:
                if not self.win32gui.IsWindowVisible(hwnd):
                    return True
                _, window_pid = self.win32process.GetWindowThreadProcessId(hwnd)
                if window_pid == self.pid and pattern.fullmatch(self.win32gui.GetWindowText(hwnd)):
                    matches.append(int(hwnd))
            except Exception:
                return True
            return True

        self.win32gui.EnumWindows(enum_callback, None)
        return matches

    def _uia_window(self, hwnd):
        self._preflight(allow_login_window=True)
        try:
            app = self.Application(backend="uia").connect(handle=hwnd)
            return app.window(handle=hwnd).wrapper_object()
        except Exception as error:
            raise self._connection_error(error) from error

    @staticmethod
    def _connection_error(error: Exception) -> WDMSError:
        # Do not include arbitrary exception text: it can contain business data.
        codes = []
        current = error
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            for attr in ("winerror", "errno", "hresult"):
                code = getattr(current, attr, None)
                if isinstance(code, int) and code not in codes:
                    codes.append(code)
            if current.args and isinstance(current.args[0], int) and current.args[0] not in codes:
                codes.append(current.args[0])
            current = current.__cause__ or current.__context__
        detail = f" 异常类型={type(error).__name__}，系统错误码={codes}。"
        if 5 in codes or -2147024891 in codes:
            return WDMSError("WDMS_ACCESS_DENIED", "Windows 拒绝访问 WDMS；请检查执行进程完整性、受限令牌和会话权限。" + detail)
        return WDMSError("WDMS_UIA_CONNECT_FAILED", "已识别 WDMS，但无法连接其窗口。" + detail)

    def _preflight(self, allow_login_window: bool = False) -> None:
        self._ensure_windows()
        if self.hwnd is None or self.pid is None or not self.win32gui.IsWindow(self.hwnd):
            raise WDMSError("WDMS_WINDOW_LOST", "WDMS 主窗口已失效，任务停止。")
        _, current_pid = self.win32process.GetWindowThreadProcessId(self.hwnd)
        if int(current_pid) != self.pid:
            raise WDMSError("WDMS_INSTANCE_CHANGED", "WDMS 主窗口所属进程发生变化。")
        try:
            is_hung = bool(self._is_hung_app_window(self.wintypes.HWND(self.hwnd)))
        except Exception as error:
            raise WDMSError("WDMS_HEALTH_CHECK_FAILED", "WDMS 响应状态检查失败。") from error
        if is_hung:
            raise WDMSError("WDMS_UNRESPONSIVE", "WDMS 已被系统标记为无响应，自动化立即停止。")
        result = self.ctypes.c_size_t()
        ok = self._send_message_timeout(
            self.wintypes.HWND(self.hwnd),
            0,
            0,
            0,
            0x0002,
            1000,
            self.ctypes.byref(result),
        )
        if not ok:
            raise WDMSError("WDMS_UNRESPONSIVE", "WDMS 有界探活未返回，自动化立即停止。")
        self._check_unexpected_modal(allow_login_window=allow_login_window)

    def _check_unexpected_modal(self, allow_login_window: bool = False) -> None:
        unexpected = []
        login_pattern = re.compile(str(self.login_cfg.get("window_title_regex", ""))) if allow_login_window and self.login_cfg.get("window_title_regex") else None

        def enum_callback(hwnd, _):
            try:
                if hwnd == self.hwnd or not self.win32gui.IsWindowVisible(hwnd):
                    return True
                _, window_pid = self.win32process.GetWindowThreadProcessId(hwnd)
                owner = self.win32gui.GetWindow(hwnd, 4)  # GW_OWNER
                if int(window_pid) == self.pid and int(owner or 0) == self.hwnd:
                    title = self.win32gui.GetWindowText(hwnd)
                    if login_pattern is None or not login_pattern.fullmatch(title):
                        unexpected.append(int(hwnd))
            except Exception:
                return True
            return True

        self.win32gui.EnumWindows(enum_callback, None)
        if unexpected:
            raise WDMSError("UNEXPECTED_MODAL_WINDOW", "WDMS 出现未配置的模态窗口，自动化停止。")

    def _find_unique(self, root, automation_id: str, required: bool = True):
        found = self._find_controls(root, [automation_id], allow_missing=not required)
        control = found.get(automation_id)
        if required and control is None:
            raise WDMSError("CONTROL_NOT_FOUND", "配置指定的 WDMS 控件不存在。")
        return control

    def _find_controls(self, root, automation_ids: Sequence[str], allow_missing: bool = False) -> Dict[str, Any]:
        targets = set(automation_ids)
        found: Dict[str, List[Any]] = {target: [] for target in targets}
        queue_items = deque([(root, 0)])
        visited = 0
        scan = self.real.get("control_scan", {})
        max_nodes = int(scan.get("max_nodes", 700))
        max_depth = int(scan.get("max_depth", 6))
        deadline = time.monotonic() + min(self.real.get("action_timeout_seconds", 45), 10)
        self._preflight(allow_login_window=True)
        try:
            while queue_items:
                if time.monotonic() >= deadline:
                    raise WDMSError("CONTROL_SCAN_TIMEOUT", "控件扫描达到本地时限；单次调用阻塞仍由父进程硬时限处理。")
                current, depth = queue_items.popleft()
                visited += 1
                if visited > max_nodes:
                    raise WDMSError("CONTROL_SCAN_LIMIT", "WDMS 控件树超过有限扫描上限。")
                self._trace_scan("scan.before_automation_id", visited=visited, depth=depth)
                automation_id = str(current.element_info.automation_id or "")
                if automation_id in targets:
                    found[automation_id].append(current)
                self._trace_scan("scan.before_control_type", visited=visited, depth=depth)
                control_type = str(getattr(current.element_info, "control_type", ""))
                # Do not expand large grids/trees: onsite scans have hung there.
                if depth < max_depth and control_type not in ("DataGrid", "DataItem", "Table", "Tree", "TreeItem"):
                    self._preflight(allow_login_window=True)
                    self._trace_scan("scan.before_children", visited=visited, depth=depth)
                    children = current.children()
                    self._trace_scan("scan.after_children", visited=visited, depth=depth)
                    self._preflight(allow_login_window=True)
                    if visited + len(queue_items) + len(children) > max_nodes:
                        raise WDMSError("CONTROL_SCAN_LIMIT", "控件扫描队列超过节点上限，停止继续展开。")
                    for child in children:
                        queue_items.append((child, depth + 1))
        except WDMSError:
            raise
        except Exception as error:
            raise WDMSError("CONTROL_SCAN_FAILED", "WDMS 有界控件定位失败。") from error
        result: Dict[str, Any] = {}
        for target in targets:
            matches = found[target]
            if len(matches) > 1:
                raise WDMSError("CONTROL_AMBIGUOUS", "配置指定的 WDMS 控件不唯一。")
            if not matches:
                if allow_missing:
                    continue
                raise WDMSError("CONTROL_NOT_FOUND", "配置指定的 WDMS 控件不存在。")
            result[target] = matches[0]
        return result

    def _trace_scan(self, step: str, **data) -> None:
        if self._scan_log is None:
            return
        try:
            with self._scan_log.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"step": step, "monotonic": time.monotonic(), **data}, ensure_ascii=False) + "\n")
                output.flush()
        except OSError as error:
            raise WDMSError("SCAN_TRACE_WRITE_FAILED", "无法写入控件诊断记录，自动化停止。") from error

    def _cache_inventory_controls(self) -> None:
        page_id = self.control_cfg.get("page_container_id", "")
        page = self._find_unique(self.main, page_id)
        targets = [
            self.control_cfg["owner_combo_id"],
            self.control_cfg["search_button_id"],
            self.control_cfg["whole_count_id"],
            self.control_cfg["loose_count_id"],
            self.evidence_cfg["result_owner_control_id"],
        ]
        if self.evidence_cfg["strategy"] == "version":
            targets.extend((self.evidence_cfg["version_control_id"], self.evidence_cfg["complete_control_id"]))
        else:
            targets.append(self.evidence_cfg["busy_control_id"])
        self.controls = self._find_controls(page, targets)

    def _signal(self, key: str) -> str:
        mapping = {
            "result_owner": (self.evidence_cfg["result_owner_control_id"], self.evidence_cfg.get("result_owner_text_source", "window_text")),
            "version": (self.evidence_cfg.get("version_control_id", ""), self.evidence_cfg.get("version_text_source", "window_text")),
            "complete": (self.evidence_cfg.get("complete_control_id", ""), self.evidence_cfg.get("complete_text_source", "window_text")),
            "busy": (self.evidence_cfg.get("busy_control_id", ""), self.evidence_cfg.get("busy_text_source", "window_text")),
        }
        automation_id, source = mapping[key]
        control = self.controls.get(automation_id)
        if control is None:
            raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "查询信号控件未能定位。")
        self._preflight()
        return self._read_source(control, source)

    @staticmethod
    def _read_source(control, source: str) -> str:
        if source == "window_text":
            value = control.window_text()
        elif source == "name":
            value = control.element_info.name
        elif source == "selected_text":
            value = control.selected_text()
        elif source == "is_enabled":
            value = control.is_enabled()
        elif source == "toggle_state":
            value = control.get_toggle_state()
        else:
            raise WDMSError("REAL_CONFIG_INCOMPLETE", "控件文本来源配置无效。")
        return str(value)

    def _read_credential(self) -> str:
        target = str(self.login_cfg.get("credential_target", ""))
        if not target:
            raise WDMSError("LOGIN_CREDENTIAL_MISSING", "Windows 凭据管理器目标名未配置。")
        try:
            import win32cred

            credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
            blob = credential.get("CredentialBlob")
            if isinstance(blob, str):
                password = blob
            elif isinstance(blob, bytes):
                encoding = str(self.login_cfg.get("credential_encoding", "utf-16-le"))
                if encoding not in ("utf-16-le", "utf-8"):
                    raise ValueError("unsupported credential encoding")
                password = blob.decode(encoding).rstrip("\x00")
            else:
                raise ValueError("credential blob missing")
        except Exception as error:
            raise WDMSError("LOGIN_CREDENTIAL_MISSING", "无法从 Windows 凭据管理器读取已配置凭据。") from error
        if not password:
            raise WDMSError("LOGIN_CREDENTIAL_MISSING", "Windows 凭据管理器中的密码为空。")
        return password

    def _require_connected(self) -> None:
        if self.main is None or self.pid is None:
            raise WDMSError("WDMS_NOT_CONNECTED", "Real 适配器尚未连接 WDMS。")
        self._preflight(allow_login_window=True)

    def _require_inventory_controls(self) -> None:
        self._require_connected()
        required = (self.control_cfg.get("owner_combo_id"), self.control_cfg.get("search_button_id"), self.control_cfg.get("whole_count_id"), self.control_cfg.get("loose_count_id"))
        if any(not control_id or control_id not in self.controls for control_id in required):
            raise WDMSError("WDMS_PAGE_UNCONFIRMED", "尚未确认在库商品检索页的必需控件。")


def serve(config: Dict[str, Any]) -> int:
    worker = WindowsWorker(config)
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            request_id = str(request.get("id", ""))
            action = str(request.get("action", ""))
            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                raise WDMSError("WORKER_PROTOCOL_INVALID", "自动化请求格式无效。")
            result = worker.dispatch(action, payload)
            response = {"id": request_id, "ok": True, "result": result}
            print(json.dumps(response, ensure_ascii=False), flush=True)
            if action == "close":
                return 0
        except WDMSError as error:
            request_id = str(locals().get("request_id", ""))
            print(json.dumps({"id": request_id, "ok": False, "error": {"code": error.code, "message": error.message}}, ensure_ascii=False), flush=True)
        except Exception:
            request_id = str(locals().get("request_id", ""))
            print(json.dumps({"id": request_id, "ok": False, "error": {"code": "REAL_ACTION_FAILED", "message": "Real 自动化动作异常失败。"}}, ensure_ascii=False), flush=True)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.config.open("r", encoding="utf-8") as file:
            config = json.load(file)
        return serve(config)
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
