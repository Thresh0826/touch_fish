"""Configuration loading. Passwords are never accepted in this file."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

from errors import WDMSError


PROJECT_ROOT = Path(__file__).resolve().parent
SAMPLE_CONFIG = PROJECT_ROOT / "config.sample.json"


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.exists():
            raise WDMSError("CONFIG_READ_FAILED", "指定的配置文件不存在。")
    else:
        path = PROJECT_ROOT / "config.json"
        if not path.exists():
            path = SAMPLE_CONFIG
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise WDMSError("CONFIG_READ_FAILED", "配置文件无法读取或格式无效。") from error
    if not isinstance(value, dict):
        raise WDMSError("CONFIG_INVALID", "配置文件顶层必须为对象。")
    value["_config_path"] = str(path.resolve())
    return value


def resolve_config_path(config: Dict[str, Any], key: str, project_root: Path) -> Path:
    configured = config.get(key)
    if not configured:
        return project_root / "output" / key
    path = Path(str(configured)).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def validate_common_config(config: Dict[str, Any]) -> None:
    timing = config.get("timing", {})
    query_timeout = timing.get("query_timeout_seconds", 15)
    total_timeout = timing.get("total_timeout_seconds", 1800)
    if not isinstance(query_timeout, (int, float)) or isinstance(query_timeout, bool) or not math.isfinite(query_timeout) or not (1 <= query_timeout <= 120):
        raise WDMSError("CONFIG_INVALID", "查询超时必须在 1 至 120 秒之间。")
    if not isinstance(total_timeout, (int, float)) or isinstance(total_timeout, bool) or not math.isfinite(total_timeout) or not (30 <= total_timeout <= 7200):
        raise WDMSError("CONFIG_INVALID", "整批任务时限必须在 30 至 7200 秒之间。")


def validate_real_config(config: Dict[str, Any]) -> None:
    """Fail closed until a Windows site has supplied and verified exact settings."""
    owners = config.get("owners")
    if not isinstance(owners, list) or len(owners) != 24:
        raise WDMSError("OWNER_LIST_UNVERIFIED", "Real 模式需要人工确认的 24 个唯一货主清单。")
    names = []
    for entry in owners:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            raise WDMSError("OWNER_LIST_UNVERIFIED", "Real 模式货主清单存在空项。")
        names.append(str(entry["name"]))
    if len(set(names)) != 24:
        raise WDMSError("OWNER_LIST_UNVERIFIED", "Real 模式货主名称必须唯一。")

    real = config.get("real", {})
    action_timeout = real.get("action_timeout_seconds", 45)
    startup_timeout = real.get("startup_timeout_seconds", 30)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not (1 <= value <= 180) for value in (action_timeout, startup_timeout)):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 动作和启动硬时限必须在 1 至 180 秒之间。")
    evidence = real.get("query_evidence", {})
    if not evidence.get("confirmed") or not evidence.get("validation_record") or not evidence.get("verified_date") or not evidence.get("client_version"):
        raise WDMSError(
            "QUERY_EVIDENCE_UNVERIFIED",
            "Windows 现场尚未确认本次查询完成与结果归属判据；Real 模式已阻断。",
        )
    if evidence.get("strategy") not in ("version", "transition"):
        raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "查询证据策略未配置或不受支持。")

    main_window = real.get("main_window", {})
    if not main_window.get("executable_path") or not main_window.get("process_name"):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式需要配置 WDMS 可执行路径和进程名。")
    login = real.get("login", {})
    login_mode = login.get("mode", "automatic")
    if login_mode not in ("automatic", "existing_session"):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "登录模式只能是 automatic 或 existing_session。")
    if login_mode == "automatic" and (not login.get("username") or not login.get("credential_target")):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式需要配置用户名及 Windows 凭据管理器目标名。")
    controls = real.get("controls", {})
    required_ids = {
        "owner_combo_id": "cmbLjq",
        "search_button_id": "btnSearch",
        "whole_count_id": "lblZjs",
        "loose_count_id": "lblSjs",
    }
    control_ids = [controls.get(key) for key in required_ids]
    if any(not isinstance(value, str) or not value.strip() for value in control_ids) or len(set(control_ids)) != 4:
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "货主、查询按钮和库存字段必须配置四个非空且唯一的 AutomationId。")
    if not controls.get("page_container_id"):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式缺少库存页面容器 AutomationId。")
    if not main_window.get("title_regex") or not main_window.get("class_name"):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式需要配置精确主窗口标题和类名。")
    steps = real.get("navigation", {}).get("steps", [])
    if len(steps) != 3 or any(not s.get("automation_id") or not s.get("page_marker_id") for s in steps):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式需要配置三步导航控件及各步页面标记。")
    strategy = real.get("owner_list_strategy")
    if strategy not in ("complete_list", "verified_name_select"):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式货主清单读取策略未配置。")
    if strategy == "verified_name_select" and not real.get("verified_name_select_confirmed"):
        raise WDMSError("OWNER_LIST_UNVERIFIED", "逐项按名称选择策略尚未现场确认。")
    if not evidence.get("result_owner_control_id"):
        raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "缺少可核验的结果货主标识控件。")
    for field in ("result_owner_text_source", "version_text_source", "complete_text_source", "busy_text_source"):
        source = evidence.get(field, "window_text")
        if source not in ("window_text", "name", "selected_text", "is_enabled", "toggle_state"):
            raise WDMSError("REAL_CONFIG_INCOMPLETE", "查询信号读取属性配置无效。")
    if evidence.get("strategy") == "version":
        if not evidence.get("version_control_id") or not evidence.get("complete_control_id") or not evidence.get("complete_value"):
            raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "版本策略需要配置版本、完成状态及其精确值。")
    else:
        if not evidence.get("busy_control_id") or evidence.get("busy_value") in (None, "") or evidence.get("idle_value") in (None, "") or evidence.get("busy_value") == evidence.get("idle_value"):
            raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "状态转换策略需要配置互不相同的忙碌与空闲值。")
    evidence_ids = [evidence["result_owner_control_id"]]
    if evidence["strategy"] == "version":
        evidence_ids.extend((evidence["version_control_id"], evidence["complete_control_id"]))
    else:
        evidence_ids.append(evidence["busy_control_id"])
    if len(set(evidence_ids)) != len(evidence_ids) or set(evidence_ids) & set(control_ids):
        raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "查询证据控件必须与货主选择、查询按钮和库存字段控件分离。")
    login_required = (
        "window_title_regex", "session_marker_id", "session_marker_value", "success_marker_id",
        "success_marker_value", "failure_marker_id", "failure_marker_value", "username_control_id",
        "password_control_id", "submit_button_id",
    )
    if login_mode == "existing_session":
        login_required = ("window_title_regex", "session_marker_id", "session_marker_value")
    if any(not login.get(field) for field in login_required):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式登录窗口、凭据控件或登录状态标记尚未配置。")
    if login.get("success_marker_scope", "main") not in ("main", "login"):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "登录成功标记作用域只能是 main 或 login。")
    for field in ("session_marker_text_source", "success_marker_text_source", "failure_marker_text_source"):
        if login.get(field, "window_text") not in ("window_text", "name", "selected_text", "is_enabled", "toggle_state"):
            raise WDMSError("REAL_CONFIG_INCOMPLETE", "登录标记文本来源配置无效。")
    scan = real.get("control_scan", {})
    for key, default, lower, upper in (("max_depth", 6, 1, 8), ("max_nodes", 700, 32, 2000)):
        value = scan.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise WDMSError("REAL_CONFIG_INCOMPLETE", "控件扫描深度或节点上限超出允许范围。")
    for pattern in (main_window["title_regex"], login["window_title_regex"]):
        try:
            import re
            re.compile(pattern)
        except (TypeError, re.error) as error:
            raise WDMSError("REAL_CONFIG_INCOMPLETE", "窗口标题正则表达式无效。") from error
    if not all(step.get("page_marker_value") for step in steps):
        raise WDMSError("REAL_CONFIG_INCOMPLETE", "Real 模式导航步骤缺少目标页面判据。")
