from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.windows_environment import require_usable_environment
from adapters.worker import WindowsWorker
from config import validate_real_config
from errors import WDMSError


def configured():
    config = json.loads((Path(__file__).resolve().parents[1] / "config.existing-session.sample.json").read_text(encoding="utf-8"))
    config["owners"] = [{"name": f"货主{i}"} for i in range(24)]
    real = config["real"]
    real["main_window"].update(executable_path="C:/WDMS/App.exe", title_regex="医药企业仓储运输管理系统", class_name="WinForm")
    real["login"].update(window_title_regex="用户登录", session_marker_id="session", session_marker_value="欢迎登录！")
    real["controls"]["page_container_id"] = "page"
    real["owner_list_strategy"] = "complete_list"
    for i, step in enumerate(real["navigation"]["steps"]):
        step.update(automation_id=f"menu{i}", page_marker_id=f"page{i}", page_marker_value=f"page{i}")
    real["query_evidence"].update(strategy="version", confirmed=True, validation_record="TEST-ONLY", verified_date="2026-10-10", client_version="TEST", result_owner_control_id="owner_result", version_control_id="version", complete_control_id="complete", complete_value="done")
    return config


class Node:
    def __init__(self, automation_id="", control_type="Pane", children=()):
        self.element_info = SimpleNamespace(automation_id=automation_id, control_type=control_type)
        self.nodes = children

    def children(self):
        if self.element_info.control_type == "Table":
            raise AssertionError("must not expand table")
        return self.nodes


class TestWindowsFixes(unittest.TestCase):
    def test_low_integrity_is_blocked_but_small_process_count_is_only_warning(self):
        with self.assertRaises(WDMSError) as caught:
            require_usable_environment({"integrity_rid": 4096, "integrity_level": "Low"})
        self.assertEqual(caught.exception.code, "ENV_INTEGRITY_TOO_LOW")
        require_usable_environment({"integrity_rid": 8192, "integrity_level": "Medium", "visible_process_count": 10})

    def test_existing_session_config_needs_no_credentials_and_ids_are_configurable(self):
        config = configured()
        config["real"]["controls"].update(owner_combo_id="custom_combo", search_button_id="custom_search", whole_count_id="custom_whole", loose_count_id="custom_loose")
        validate_real_config(config)
        config["real"]["login"]["mode"] = "automatic"
        with self.assertRaises(WDMSError):
            validate_real_config(config)

    def test_evidence_cannot_reuse_custom_condition_control(self):
        config = configured()
        config["real"]["controls"]["owner_combo_id"] = "custom_combo"
        config["real"]["query_evidence"]["result_owner_control_id"] = "custom_combo"
        with self.assertRaises(WDMSError) as caught:
            validate_real_config(config)
        self.assertEqual(caught.exception.code, "QUERY_EVIDENCE_UNVERIFIED")

    def test_manual_login_mode_does_not_enter_credentials_branch(self):
        worker = WindowsWorker(configured())
        worker._require_connected = lambda: None
        worker._preflight = lambda **kw: None
        worker._login_windows = lambda: [42]
        worker._read_credential = mock.Mock(side_effect=AssertionError("must not read credentials"))
        with self.assertRaises(WDMSError) as caught:
            worker.login({})
        self.assertEqual(caught.exception.code, "LOGIN_REQUIRED")
        worker._read_credential.assert_not_called()

    def test_login_success_marker_can_appear_later_in_login_scope(self):
        config = configured()
        config["real"]["login"].update(mode="automatic", username="test", credential_target="test", success_marker_scope="login", success_marker_id="success", success_marker_value="done", failure_marker_id="failure", failure_marker_value="rejected", username_control_id="user", password_control_id="password", submit_button_id="submit", timeout_seconds=1)
        worker = WindowsWorker(config)
        worker.main = object()
        worker._require_connected = lambda: None
        worker._preflight = lambda **kw: None
        worker._login_windows = lambda: [42]
        login_root = object()
        worker._uia_window = lambda hwnd: login_root
        worker._read_credential = lambda: "TEST-ONLY"
        edit = SimpleNamespace(set_edit_text=lambda text: None)
        worker._find_controls = lambda root, ids: {"user": edit, "password": edit, "submit": SimpleNamespace(invoke=lambda: None), "failure": SimpleNamespace(window_text=lambda: "")}
        marker = SimpleNamespace(window_text=lambda: "done")
        lookup = mock.Mock(side_effect=[None, marker])
        worker._find_unique = lookup
        with mock.patch("adapters.worker.time.sleep"):
            self.assertEqual(worker.login({})["branch"], "credentials")
        self.assertEqual(lookup.call_count, 2)
        self.assertIs(lookup.call_args.args[0], login_root)

    def test_depth_six_control_found_and_table_not_expanded(self):
        worker = WindowsWorker(configured())
        worker._preflight = lambda **kw: None
        target = Node("wanted")
        branch = target
        for _ in range(5):
            branch = Node(children=[branch])
        root = Node(children=[branch, Node("grid", "Table")])
        self.assertIs(worker._find_unique(root, "wanted"), target)
        self.assertEqual(worker._find_controls(root, ["grid"])["grid"].element_info.control_type, "Table")

    def test_scan_rejects_duplicate_and_node_overflow(self):
        worker = WindowsWorker(configured())
        worker._preflight = lambda **kw: None
        for root, code in ((Node(children=[Node("x"), Node("x")]), "CONTROL_AMBIGUOUS"), (Node(children=[Node() for _ in range(701)]), "CONTROL_SCAN_LIMIT")):
            with self.subTest(code=code):
                with self.assertRaises(WDMSError) as caught:
                    worker._find_controls(root, ["x"])
                self.assertEqual(caught.exception.code, code)

    def test_access_denied_detail_keeps_numeric_code_without_raw_text(self):
        error = OSError(5, "SENSITIVE-RAW-TEXT")
        wrapped = WindowsWorker._connection_error(error)
        self.assertEqual(wrapped.code, "WDMS_ACCESS_DENIED")
        self.assertIn("5", wrapped.message)
        self.assertNotIn("SENSITIVE", wrapped.message)

    def test_busy_transition_observed_while_old_result_owner_is_visible(self):
        worker = WindowsWorker(configured())
        worker.evidence_cfg.update(strategy="transition", busy_control_id="busy", busy_value="busy", idle_value="idle")
        worker._require_inventory_controls = lambda: None
        worker._preflight = lambda **kw: None
        worker.controls = {
            "cmbLjq": SimpleNamespace(selected_text=lambda: "货主1"),
            "lblZjs": SimpleNamespace(window_text=lambda: "120"),
            "lblSjs": SimpleNamespace(window_text=lambda: "8"),
        }
        worker.pending = {"request_id": "req", "owner_name": "货主1", "owner_id": "1", "evidence_before": {}}
        owners = iter(["旧货主", "货主1", "货主1", "货主1"])
        states = iter(["busy", "idle", "idle"])
        worker._signal = lambda key: next(owners) if key == "result_owner" else next(states)
        with mock.patch("adapters.worker.time.sleep"):
            result = worker.read_result({"request_id": "req", "owner_name": "货主1", "timeout_seconds": 2})
        self.assertEqual(result["whole_count"], "120")
        self.assertEqual(result["evidence"]["completion"], "idle")


if __name__ == "__main__":
    unittest.main()
