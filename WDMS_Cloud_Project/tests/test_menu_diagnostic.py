import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from errors import WDMSError
from scripts import diagnose_wdms_menu as diagnostic


def window(hwnd, *, pid=7, visible=True, class_name="WindowsForms10.Window.8.app",
           rect=None, depth=1):
    return {
        "hwnd": hwnd,
        "pid": pid,
        "visible": visible,
        "class_name": class_name,
        "rect": list(rect or (0, 0, 100, 30)),
        "depth": depth,
    }


def request(containers=None):
    return {
        "schema_version": 1,
        "root_hwnd": 100,
        "pid": 7,
        "containers": list(containers or [{
            "hwnd": 200,
            "class_name": "WindowsForms10.Window.8.app",
            "rect": [0, 0, 200, 30],
            "depth": 1,
        }]),
    }


def match(target="在库管理", *, hwnd=200, path=None, path_kind="child_id", role=12, state=0):
    return {
        "target": target,
        "container_hwnd": hwnd,
        "child_path": list(path or [1]),
        "path_kind": path_kind,
        "role": role,
        "state": state,
    }


def result_events(req, matches=None):
    events = []
    for container in req["containers"]:
        events.append({"event": "menu_msaa_container_started", "container_hwnd": container["hwnd"]})
        events.append({
            "event": "menu_msaa_container_complete",
            "container_hwnd": container["hwnd"],
            "matches_count": sum(1 for item in (matches or []) if item["container_hwnd"] == container["hwnd"]),
        })
    events.append({
        "event": "menu_msaa_complete",
        "schema_version": 1,
        "matches": list(matches or []),
        "nodes": 4,
        "elapsed_ms": 12,
        "containers_completed": len(req["containers"]),
        "truncated": False,
    })
    return events


class FakeApi:
    def __init__(self, nodes, children):
        self.nodes = nodes
        self.children = children
        self.child_calls = []
        self.metadata_calls = []

    def child_handles(self, hwnd):
        self.child_calls.append(hwnd)
        if hwnd not in self.children:
            raise AssertionError("forbidden or unexpected window recursion")
        return iter(self.children[hwnd])

    def metadata(self, hwnd):
        self.metadata_calls.append(hwnd)
        return dict(self.nodes[hwnd])


class MenuCandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = {"hwnd": 100, "pid": 7}

    def test_candidates_filter_pid_visibility_class_and_root_then_sort_and_deduplicate(self):
        nodes = [
            window(1, depth=2, rect=(0, 20, 50, 40)),
            window(2, depth=1, rect=(20, 10, 70, 30)),
            window(3, depth=1, rect=(30, 5, 80, 25)),
            window(3, depth=1, rect=(30, 5, 80, 25)),
            window(4, pid=8),
            window(5, visible=False),
            window(6, class_name="WindowsForms10.BUTTON.app"),
            window(100),
        ]
        selected = diagnostic.select_candidates(nodes, self.root)
        self.assertEqual([item["hwnd"] for item in selected], [3, 2, 1])
        self.assertEqual(set(selected[0]), {"hwnd", "class_name", "rect", "depth"})

    def test_more_than_sixteen_candidates_stops_without_truncating(self):
        nodes = [window(hwnd) for hwnd in range(200, 217)]
        with self.assertRaises(WDMSError) as caught:
            diagnostic.select_candidates(nodes, self.root)
        self.assertEqual(caught.exception.code, "MENU_CONTAINER_LIMIT")

    def test_candidates_beyond_three_levels_do_not_consume_candidate_limit(self):
        nodes = [window(hwnd, depth=depth) for depth, hwnd in enumerate((200, 201, 202), start=1)]
        nodes.extend(window(hwnd, depth=4 + (index % 5)) for index, hwnd in enumerate(range(300, 317)))
        selected = diagnostic.select_candidates(nodes, self.root)
        self.assertEqual([item["hwnd"] for item in selected], [200, 201, 202])
        self.assertEqual([item["depth"] for item in selected], [1, 2, 3])

    def test_no_candidate_stops(self):
        with self.assertRaises(WDMSError) as caught:
            diagnostic.select_candidates([window(1, visible=False)], self.root)
        self.assertEqual(caught.exception.code, "MENU_CONTAINER_NOT_FOUND")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = {"hwnd": 100, "pid": 7, "visible": True, "minimized": False}
        self.settings = SimpleNamespace(wdms_exe="C:\\WDMS\\App.exe", username="private-user")

    def make_tree(self, *, with_mdi=True, with_marker=True, deep=False, many=0):
        nodes = {}
        children = {100: []}
        labels = {}
        next_hwnd = 200

        if with_mdi:
            nodes[10] = window(10, class_name="MDICLIENT", rect=(0, 0, 500, 300))
            children[100].append(10)
            children[10] = []
        if with_marker:
            nodes[11] = window(11, class_name="WindowsForms10.STATIC.app", rect=(2, 2, 100, 20))
            children[100].append(11)
            labels[11] = diagnostic.HOME_MARKER
        nodes[next_hwnd] = window(next_hwnd, rect=(10, 25, 250, 60), depth=1)
        first_container = next_hwnd
        children[100].append(first_container)
        children[first_container] = []
        next_hwnd += 1

        # These controls must never be expanded, even if a fake tree has children for them.
        for class_name in ("WindowsForms10.EDIT.app", "WindowsForms10.COMBOBOX.app", "WindowsForms10.Button.app"):
            nodes[next_hwnd] = window(next_hwnd, class_name=class_name)
            children[100].append(next_hwnd)
            children[next_hwnd] = [9000 + next_hwnd]
            next_hwnd += 1

        if deep:
            parent = first_container
            for depth in (2, 3, 4):
                nodes[next_hwnd] = window(next_hwnd, depth=depth)
                children[parent].append(next_hwnd)
                children[next_hwnd] = []
                parent = next_hwnd
                next_hwnd += 1

        for index in range(many):
            hwnd = 1000 + index
            nodes[hwnd] = window(hwnd, class_name="WindowsForms10.STATIC.app", visible=False)
            children[100].append(hwnd)
            children[hwnd] = []
        return FakeApi(nodes, children), labels, first_container

    def call_discover(self, api, labels, request_path, *, clock=None):
        log = Path(self.temp.name) / "discovery.jsonl"
        label_calls = []

        def read_label(fake_api, node):
            label_calls.append(node["hwnd"])
            return labels.get(node["hwnd"], "PRIVATE_OTHER_CAPTION")

        patches = [
            mock.patch.object(diagnostic, "load_settings", return_value=self.settings),
            mock.patch.object(diagnostic, "validate_executable", return_value="C:\\WDMS\\App.exe"),
            mock.patch.object(diagnostic, "LoginNativeWindows", return_value=api),
            mock.patch.object(diagnostic, "find_stage_window", return_value=self.root),
            mock.patch.object(diagnostic, "pid_matches", return_value=True),
            mock.patch.object(diagnostic, "_read_visible_static_label", side_effect=read_label),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        if clock is not None:
            patch = mock.patch.object(diagnostic.time, "monotonic", side_effect=clock)
            patch.start()
            self.addCleanup(patch.stop)
        value = diagnostic.discover(request_path, log)
        return value, log, label_calls

    def test_fake_tree_confirms_home_and_only_recurses_allowed_containers(self):
        api, labels, first = self.make_tree()
        labels[11] = diagnostic.HOME_MARKER
        api.nodes[12] = window(12, class_name="WindowsForms10.STATIC.app")
        api.children[first].append(12)
        api.children[12] = [9999]
        labels[12] = "PRIVATE_OTHER_CAPTION"
        request_path = Path(self.temp.name) / "request.json"

        value, log, label_calls = self.call_discover(api, labels, request_path)

        self.assertEqual(api.child_calls, [100, first])
        self.assertEqual(label_calls, [11, 12])
        self.assertEqual([item["hwnd"] for item in value["containers"]], [first])
        self.assertEqual(set(value), {"schema_version", "root_hwnd", "pid", "containers"})
        self.assertEqual(set(value["containers"][0]), {"hwnd", "class_name", "rect", "depth"})
        self.assertNotIn("private-user", request_path.read_text(encoding="utf-8"))
        self.assertNotIn("C:\\WDMS\\App.exe", request_path.read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE_OTHER_CAPTION", log.read_text(encoding="utf-8"))
        self.assertIn(diagnostic.HOME_MARKER, log.read_text(encoding="utf-8"))

    def test_missing_either_home_marker_stops_before_request(self):
        for with_mdi, with_marker in ((False, True), (True, False)):
            with self.subTest(with_mdi=with_mdi, with_marker=with_marker):
                api, labels, _ = self.make_tree(with_mdi=with_mdi, with_marker=with_marker)
                if with_marker:
                    labels[11] = diagnostic.HOME_MARKER
                path = Path(self.temp.name) / f"request-{with_mdi}-{with_marker}.json"
                with mock.patch.object(diagnostic, "load_settings", return_value=self.settings), \
                     mock.patch.object(diagnostic, "validate_executable", return_value="C:\\WDMS\\App.exe"), \
                     mock.patch.object(diagnostic, "LoginNativeWindows", return_value=api), \
                     mock.patch.object(diagnostic, "find_stage_window", return_value=self.root), \
                     mock.patch.object(diagnostic, "pid_matches", return_value=True), \
                     mock.patch.object(diagnostic, "_read_visible_static_label", side_effect=lambda _api, node: labels.get(node["hwnd"])):
                    with self.assertRaises(WDMSError) as caught:
                        diagnostic.discover(path, Path(self.temp.name) / "missing-home.jsonl")
                self.assertEqual(caught.exception.code, "MENU_HOME_UNCONFIRMED")
                self.assertFalse(path.exists())

    def test_window_node_budget_stops_without_request(self):
        api, labels, _ = self.make_tree(with_mdi=False, with_marker=False, many=96)
        path = Path(self.temp.name) / "too-many.json"
        with mock.patch.object(diagnostic, "load_settings", return_value=self.settings), \
             mock.patch.object(diagnostic, "validate_executable", return_value="C:\\WDMS\\App.exe"), \
             mock.patch.object(diagnostic, "LoginNativeWindows", return_value=api), \
             mock.patch.object(diagnostic, "find_stage_window", return_value=self.root), \
             mock.patch.object(diagnostic, "pid_matches", return_value=True):
            with self.assertRaises(WDMSError) as caught:
                diagnostic.discover(path, Path(self.temp.name) / "node-limit.jsonl")
        self.assertEqual(caught.exception.code, "MENU_NODE_LIMIT")
        self.assertFalse(path.exists())

    def test_depth_eight_home_marker_confirms_but_only_first_three_levels_are_candidates(self):
        nodes = {
            10: window(10, class_name="MDICLIENT"),
        }
        children = {100: [10, 200]}
        labels = {}
        for depth, hwnd in enumerate(range(200, 209), start=1):
            nodes[hwnd] = window(hwnd, depth=depth)
            children[hwnd] = []
            if depth < 9:
                children[hwnd].append(hwnd + 1)
        marker = 800
        nodes[marker] = window(marker, class_name="WindowsForms10.STATIC.app", depth=8)
        children[206].append(marker)
        labels[marker] = "       用户密码修改"
        api = FakeApi(nodes, children)
        request_path = Path(self.temp.name) / "depth-eight.json"

        value, _, label_calls = self.call_discover(api, labels, request_path)

        self.assertIn(marker, label_calls)
        self.assertEqual([item["hwnd"] for item in value["containers"]], [200, 201, 202])
        self.assertEqual([item["depth"] for item in value["containers"]], [1, 2, 3])
        self.assertEqual(api.child_calls, [100, 200, 201, 202, 203, 204, 205, 206])
        self.assertNotIn(207, api.child_calls)
        self.assertNotIn(208, api.metadata_calls)
        self.assertNotIn(208, api.child_calls)
        self.assertEqual(api.child_calls.count(100), 1)

    def test_missing_depth_eight_home_marker_stops_without_request(self):
        nodes = {10: window(10, class_name="MDICLIENT")}
        children = {100: [10, 200]}
        labels = {}
        for depth, hwnd in enumerate(range(200, 209), start=1):
            nodes[hwnd] = window(hwnd, depth=depth)
            children[hwnd] = []
            if depth < 9:
                children[hwnd].append(hwnd + 1)
        marker = 800
        nodes[marker] = window(marker, class_name="WindowsForms10.STATIC.app", depth=8)
        children[206].append(marker)
        labels[marker] = "其他标签"
        api = FakeApi(nodes, children)
        path = Path(self.temp.name) / "depth-eight-missing-marker.json"
        with mock.patch.object(diagnostic, "load_settings", return_value=self.settings), \
             mock.patch.object(diagnostic, "validate_executable", return_value="C:\\WDMS\\App.exe"), \
             mock.patch.object(diagnostic, "LoginNativeWindows", return_value=api), \
             mock.patch.object(diagnostic, "find_stage_window", return_value=self.root), \
             mock.patch.object(diagnostic, "pid_matches", return_value=True), \
             mock.patch.object(diagnostic, "_read_visible_static_label", side_effect=lambda _api, node: labels.get(node["hwnd"])):
            with self.assertRaises(WDMSError) as caught:
                diagnostic.discover(path, Path(self.temp.name) / "depth-eight-missing-marker.jsonl")
        self.assertEqual(caught.exception.code, "MENU_HOME_UNCONFIRMED")
        self.assertFalse(path.exists())
        self.assertNotIn(208, api.metadata_calls)
        self.assertNotIn(208, api.child_calls)

    def test_three_second_cooperative_deadline_stops_discovery(self):
        api, labels, _ = self.make_tree()
        labels[11] = diagnostic.HOME_MARKER
        values = iter((0.0, 3.1))
        path = Path(self.temp.name) / "timeout.json"
        with mock.patch.object(diagnostic, "load_settings", return_value=self.settings), \
             mock.patch.object(diagnostic, "validate_executable", return_value="C:\\WDMS\\App.exe"), \
             mock.patch.object(diagnostic, "LoginNativeWindows", return_value=api), \
             mock.patch.object(diagnostic, "find_stage_window", return_value=self.root), \
             mock.patch.object(diagnostic, "pid_matches", return_value=True), \
             mock.patch.object(diagnostic.time, "monotonic", side_effect=lambda: next(values)):
            with self.assertRaises(WDMSError) as caught:
                diagnostic.discover(path, Path(self.temp.name) / "timeout.jsonl")
        self.assertEqual(caught.exception.code, "MENU_DISCOVERY_TIME_LIMIT")
        self.assertFalse(path.exists())


class ResultValidationTests(unittest.TestCase):
    def traced_events(self):
        events = result_events(self.request)
        traces = [{"event": "menu_msaa_node", "node": i + 1,
                   "container_hwnd": 200, "depth": 1, "child_path": [i],
                   "path_kind": "index_or_child_id", "node_kind": "object",
                   "role": 9 if i < 3 else None} for i in range(4)]
        return [events[0], *traces, *events[1:]]

    def test_accepts_structure_trace_including_window_wrappers_and_unknown_role(self):
        self.assertEqual(diagnostic.validate_msaa_events(self.traced_events(), self.request)["nodes"], 4)

    def test_rejects_inconsistent_or_private_structure_trace(self):
        for changes in ({"node": 9}, {"depth": 5}, {"container_hwnd": 201},
                        {"child_path": [True]}, {"role": True}, {"name": "private caption"}):
            events = self.traced_events()
            events[1].update(changes)
            with self.subTest(changes=changes):
                self.assert_invalid(events)
        events = self.traced_events()
        events.pop(4)
        self.assert_invalid(events)

    def test_pruned_branches_or_partial_summary_never_count_as_complete(self):
        for item in ({"event": "menu_msaa_branch_pruned", "kind": "depth"},
                     {"event": "menu_msaa_partial", "kind": "depth", "matches_so_far": [match()]}):
            events = result_events(self.request)
            self.assert_invalid([*events[:-1], item, events[-1]])

    def test_container_quota_pruning_never_counts_as_complete(self):
        events = result_events(self.request)
        item = {"event": "menu_msaa_container_pruned", "kind": "container_node_count",
                "container_hwnd": self.request['containers'][0]['hwnd'],
                "node_budget": 14, "nodes": 14}
        self.assert_invalid([*events[:-1], item, events[-1]])

    def test_container_truncation_still_rejected_if_pruning_event_is_missing(self):
        for value in (True, 1, None, "false"):
            events = result_events(self.request)
            events[1]['truncated'] = value
            with self.subTest(value=value):
                self.assert_invalid(events)

    def test_complete_client_wrapper_structure_is_accepted(self):
        events = self.traced_events()
        events[2]['role'] = 4
        container = next(e for e in events if e['event'] == 'menu_msaa_container_complete')
        container.update(truncated=False, client_wrappers=1, node_budget=128,
                         probe_revision='role-summary-v3-client-wrapper')
        self.assertEqual(diagnostic.validate_msaa_events(events, self.request)['nodes'], 4)

    def test_preserves_allowed_container_name(self):
        req = request()
        matches = [match(target="Tsp_顶部子系统菜单", role=22)]
        result = diagnostic.validate_msaa_events(result_events(req, matches), req)
        self.assertEqual(result["matches"], matches)

    def setUp(self):
        self.request = request()

    def assert_invalid(self, events):
        with self.assertRaises(WDMSError):
            diagnostic.validate_msaa_events(events, self.request)

    def test_rejects_missing_or_duplicate_completion_and_limit_records(self):
        complete = result_events(self.request)
        self.assert_invalid(complete[:-1])
        self.assert_invalid(complete + [dict(complete[-1])])
        self.assert_invalid(complete[:-1] + [{"event": "menu_msaa_limit", "kind": "time"}, complete[-1]])
        self.assert_invalid(complete[:-1] + [{"event": "menu_msaa_cancelled"}, complete[-1]])
        self.assert_invalid(complete[:-1] + [{"event": "incomplete_log_record"}, complete[-1]])

    def test_rejects_invalid_target_and_non_request_container(self):
        self.assert_invalid(result_events(self.request, [match(target="用户密码修改")]))
        self.assert_invalid(result_events(self.request, [match(hwnd=201)]))

    def test_rejects_noninteger_or_overlong_paths_and_noninteger_role_state(self):
        for item in (
            match(path=["1"]),
            match(path=[True]),
            match(path=[1, 2, 3, 4, 5, 6]),
            match(role=True),
            match(state="0"),
        ):
            with self.subTest(item=item):
                self.assert_invalid(result_events(self.request, [item]))

    def test_accepts_zero_matches_and_multiple_management_matches_as_read_only_data(self):
        self.assertEqual(diagnostic.validate_msaa_events(result_events(self.request), self.request)["matches"], [])
        duplicate = [match(path=[1]), match(path=[2])]
        self.assertEqual(len(diagnostic.validate_msaa_events(result_events(self.request, duplicate), self.request)["matches"]), 2)

    def test_accepts_role_summary_with_zero_matches_but_requires_final_completion(self):
        req = request()
        events = result_events(req, [])
        events[1].update({
            "probe_revision": "role-summary-v1",
            "roles": {"10": 1, "42": 1},
            "role_unavailable": 0,
            "filtered_nodes": 1,
            "object_nodes": 1,
            "element_nodes": 1,
            "root_role": 10,
            "root_child_count": 1,
        })
        result = diagnostic.validate_msaa_events(events, req)
        self.assertEqual(result["matches"], [])

        incomplete = [dict(event) for event in events]
        incomplete.pop()
        with self.assertRaises(WDMSError):
            diagnostic.validate_msaa_events(incomplete, req)

    def test_rejects_completed_container_before_start_and_overlapping_containers(self):
        events = result_events(self.request)
        self.assert_invalid([events[1], events[0], events[2]])
        req = request([request()["containers"][0], {**request()["containers"][0], "hwnd": 201}])
        events = result_events(req)
        with self.assertRaises(WDMSError):
            diagnostic.validate_msaa_events([events[0], events[2], events[1], events[3], events[4]], req)

    def test_rejects_mismatched_container_counts_and_impossible_node_counts(self):
        events = result_events(self.request, [match()])
        events[1]["matches_count"] = 0
        self.assert_invalid(events)
        for nodes in (0, 129):
            events = result_events(self.request)
            events[-1]["nodes"] = nodes
            self.assert_invalid(events)
        self.assert_invalid(result_events(self.request, [match(path=[i]) for i in range(5)]))

    def test_rejects_unapproved_role_and_negative_state(self):
        for item in (match(role=42), match(state=-1)):
            self.assert_invalid(result_events(self.request, [item]))


class ParentProcessTests(unittest.TestCase):
    def test_discovery_interrupt_records_failure_and_returns_two(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "diagnostic.jsonl"
            with mock.patch.object(diagnostic, "discover", side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = diagnostic._discover_entry(Path(folder) / "request.json", log)
            events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["event"], "menu_diagnostic_failed")
        self.assertEqual(events[-1]["stage"], "discover")
        self.assertIn("discover", output.getvalue())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "out"
        self.system_root = Path(self.temp.name) / "Windows"
        self.request = request()

    def run_parent(self, matches=None, *, first_code=0, second_code=0,
                   request_ready=True, second_status_event=None):
        calls = []

        def supervise(command, log, timeout, cancelled=None):
            calls.append((list(command), Path(log), timeout, cancelled))
            if len(calls) == 1:
                if request_ready:
                    request_argument = Path(command[command.index("--request") + 1])
                    request_argument.write_text(json.dumps(self.request), encoding="utf-8")
                return first_code
            if second_code == 0 or second_code != 0:
                output_path = Path(command[command.index("-OutputFile") + 1])
                output_path.write_text(
                    "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in result_events(self.request, matches)),
                    encoding="utf-8",
                )
                if second_status_event:
                    diagnostic.probe.record(log, second_status_event, error_code=second_status_event.upper())
            return second_code

        output = io.StringIO()
        with mock.patch.object(diagnostic.sys, "platform", "win32"), \
             mock.patch.dict(os.environ, {"SystemRoot": str(self.system_root)}), \
             mock.patch.object(diagnostic.probe, "supervise", side_effect=supervise), \
             contextlib.redirect_stdout(output):
            code = diagnostic.main(["--out", str(self.out)])
        logs = list(self.out.glob("menu_*/diagnostic.jsonl")) if self.out.exists() else []
        events = diagnostic.probe.read_events(logs[0]) if logs else []
        return code, calls, events, output.getvalue()

    def test_discovery_failure_never_starts_powershell(self):
        code, calls, events, _ = self.run_parent(first_code=2)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], 10)
        self.assertFalse(any(item["event"] == "menu_diagnostic_complete" for item in events))

    def test_frozen_discovery_routes_through_executable_worker(self):
        with mock.patch.object(diagnostic.sys, 'frozen', True, create=True):
            code, calls, _, _ = self.run_parent(first_code=2)
        self.assertEqual(code, 2)
        self.assertEqual(calls[0][0][:3], [diagnostic.sys.executable, '--menu-diagnostic', '--_discover'])

    def test_successful_discovery_requires_complete_request_before_powershell(self):
        code, calls, events, _ = self.run_parent(request_ready=False)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(item["event"] == "menu_diagnostic_complete" for item in events))

    def test_cancel_or_hard_timeout_stops_without_diagnostic_completion(self):
        for status in ("probe_interrupted", "probe_timeout"):
            with self.subTest(status=status):
                code, calls, events, _ = self.run_parent(second_code=2, second_status_event=status)
                self.assertEqual(code, 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0][2], 10)
                self.assertEqual(calls[1][2], 20)
                self.assertFalse(any(item["event"] == "menu_diagnostic_complete" for item in events))

    def test_powershell_path_sta_and_arguments_are_fixed(self):
        code, calls, _, _ = self.run_parent()
        self.assertEqual(code, 0)
        powershell = Path(self.system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        command = calls[1][0]
        self.assertEqual(command[0], str(powershell))
        self.assertEqual(command[1:7], ["-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(command[8], "-RequestFile")
        self.assertEqual(Path(command[9]).name, "request.json")
        self.assertEqual(command[10], "-OutputFile")
        self.assertEqual(Path(command[11]).name, "msaa.jsonl")
        self.assertEqual(calls[1][2], 20)

    def test_zero_matches_complete_only_read_only_diagnostic(self):
        code, _, events, _ = self.run_parent(matches=[])
        self.assertEqual(code, 0)
        names = [item["event"] for item in events]
        self.assertIn("menu_target_not_exposed", names)
        complete = next(item for item in events if item["event"] == "menu_diagnostic_complete")
        self.assertTrue(complete["read_only"])
        self.assertFalse(complete["query_performed"])
        self.assertFalse(complete["click_performed"])
        self.assertNotIn("automatic_navigation_complete", names)

    def test_multiple_management_matches_are_ambiguous_without_action(self):
        code, _, events, _ = self.run_parent(matches=[match(path=[1]), match(path=[2])])
        self.assertEqual(code, 0)
        ambiguous = next(item for item in events if item["event"] == "menu_target_ambiguous")
        self.assertEqual(ambiguous["matches"], 2)
        self.assertNotIn("automatic_navigation_complete", [item["event"] for item in events])


if __name__ == "__main__":
    unittest.main()
