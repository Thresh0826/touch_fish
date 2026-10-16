from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from errors import NativeTextReadError, WDMSError
from scripts import check_wdms_window as probe
from adapters.worker import WindowsWorker


class FakeNative:
    def __init__(self, matches=None, fail_health=False):
        self.matches = matches if matches is not None else [{"hwnd": 1, "pid": 10, "minimized": False}]
        self.fail_health = fail_health
        self.health_calls = []

    def find_main(self, title, log):
        return self.matches

    def health(self, hwnd, log, stage):
        self.health_calls.append(stage)
        if self.fail_health:
            raise WDMSError("WDMS_UNRESPONSIVE", "test-only")

    def child_handles(self, hwnd):
        raise AssertionError("default probe must never enumerate children")


class TestWindowProbe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "probe.jsonl"

    def args(self, **changes):
        args = SimpleNamespace(log=str(self.log), title="WDMS", native_controls=False)
        args.__dict__.update(changes)
        return args

    def run_fake(self, api):
        with mock.patch.object(probe, "inspect_environment", return_value={"integrity_rid": 8192}), mock.patch.object(probe, "NativeWindows", return_value=api):
            return probe.child_main(self.args())

    def test_default_checks_top_level_health_and_never_scans_children(self):
        api = FakeNative()
        self.assertEqual(self.run_fake(api), 0)
        self.assertEqual(api.health_calls, ["initial", "final"])
        success = probe.read_events(self.log)[-1]
        self.assertFalse(success["uia_scan_performed"])
        self.assertFalse(success["inventory_collection_performed"])

    def test_unresponsive_window_is_failure_not_zero_controls_success(self):
        self.assertEqual(self.run_fake(FakeNative(fail_health=True)), 2)
        events = probe.read_events(self.log)
        self.assertEqual(events[-1]["error_code"], "WDMS_UNRESPONSIVE")
        self.assertFalse(any(item["event"] == "probe_success" for item in events))

    def test_missing_or_ambiguous_or_minimized_main_stops_before_health(self):
        for matches, code in (([], "WDMS_WINDOW_NOT_FOUND"), ([{}, {}], "WDMS_INSTANCE_AMBIGUOUS"), ([{"minimized": True}], "WDMS_WINDOW_MINIMIZED")):
            with self.subTest(code=code):
                api = FakeNative(matches=matches)
                self.assertEqual(self.run_fake(api), 2)
                self.assertEqual(probe.read_events(self.log)[-1]["error_code"], code)
                self.assertEqual(api.health_calls, [])

    def test_killed_child_preserves_last_step_and_is_reaped(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        probe.record(self.log, "before_native_api")
        original_popen = subprocess.Popen
        created = []

        def start_child(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            created.append(child)
            return child

        start = time.monotonic()
        with mock.patch.object(probe.subprocess, "Popen", side_effect=start_child):
            self.assertEqual(probe.supervise(command, self.log, timeout=0.2), 2)
        self.assertLess(time.monotonic() - start, 2)
        events = probe.read_events(self.log)
        self.assertEqual(events[-1]["error_code"], "PROBE_HARD_TIMEOUT")
        pid = next(item["pid"] for item in events if item["event"] == "child_started")
        self.assertEqual(created[0].pid, pid)
        self.assertIsNotNone(created[0].poll())

    def test_incomplete_final_log_does_not_hide_earlier_evidence(self):
        probe.record(self.log, "before_blocking_call")
        with self.log.open("a", encoding="utf-8") as output:
            output.write('{"event":')
        events = probe.read_events(self.log)
        self.assertEqual(events[0]["event"], "before_blocking_call")
        self.assertEqual(events[1]["event"], "incomplete_log_record")

    def test_worker_exit_code_and_stderr_survive_failure_before_child_logging(self):
        stderr=self.log.with_suffix('.stderr.txt')
        command=[sys.executable,'-c',"import sys; sys.stderr.write('synthetic startup failure\\n'); sys.exit(7)"]
        self.assertEqual(probe.supervise(command,self.log,3,stderr_path=stderr),7)
        self.assertEqual(stderr.read_text(encoding='utf-8'),'synthetic startup failure\n')
        final=probe.read_events(self.log)[-1]
        self.assertEqual((final['event'],final['exit_code']),('child_exited',7))

    def test_stderr_capture_does_not_overwrite_existing_evidence(self):
        stderr=self.log.with_suffix('.stderr.txt');stderr.write_text('previous evidence', encoding='utf-8')
        with mock.patch.object(probe.subprocess,'Popen') as popen:
            self.assertEqual(probe.supervise(['worker'],self.log,3,stderr_path=stderr),2)
        popen.assert_not_called();self.assertEqual(stderr.read_text(encoding='utf-8'),'previous evidence')
        self.assertEqual(probe.read_events(self.log)[-1]['event'],'probe_start_failed')

    def test_old_search_flag_is_rejected_before_any_windows_action(self):
        script = probe.PROJECT_ROOT / "scripts/check_wdms_window.py"
        result = subprocess.run([sys.executable, str(script), "--invoke-search"], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_worker_scan_trace_identifies_the_failing_call_without_data(self):
        worker = WindowsWorker({"real": {"main_window": {}, "login": {}, "controls": {}, "query_evidence": {}}})
        worker._scan_log = self.log
        worker._preflight = lambda **kwargs: None
        root = SimpleNamespace(element_info=SimpleNamespace(automation_id="", control_type="Pane"), children=mock.Mock(side_effect=RuntimeError("raw-sensitive-value")))
        with self.assertRaises(WDMSError) as caught:
            worker._find_controls(root, ["target"])
        self.assertEqual(caught.exception.code, "CONTROL_SCAN_FAILED")
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[-1]["step"], "scan.before_children")
        self.assertNotIn("raw-sensitive", self.log.read_text(encoding="utf-8"))

    def test_optional_native_scan_has_node_limit_and_avoids_child_text(self):
        api = SimpleNamespace(health=lambda *args: None, child_handles=lambda hwnd: iter([2, 3]), metadata=lambda hwnd: {"hwnd": hwnd, "pid": 10, "class_name": "Grid"})
        with self.assertRaises(WDMSError) as caught:
            probe.check_native_controls(api, {"hwnd": 1, "pid": 10}, self.log, max_nodes=1)
        self.assertEqual(caught.exception.code, "NATIVE_SCAN_LIMIT")
        entries = probe.read_events(self.log)
        self.assertEqual(sum(item["event"] == "native_control" for item in entries), 1)
        self.assertFalse(any("title" in item for item in entries))

    def test_text_allowlist_excludes_edits_tables_hidden_and_other_windows(self):
        for kind in ("STATIC", "BUTTON", "COMBOBOX"):
            self.assertTrue(probe.text_read_allowed({"visible": True, "class_name": f"WindowsForms10.{kind}.app.0.test"}))
        for kind in ("EDIT", "Window", "DataGrid", "TABLE"):
            self.assertFalse(probe.text_read_allowed({"visible": True, "class_name": f"WindowsForms10.{kind}.app.0.test"}))
        self.assertFalse(probe.text_read_allowed({"visible": False, "class_name": "WindowsForms10.STATIC.app.0.test"}))

    def test_text_scan_reads_only_allowlisted_controls(self):
        reader = mock.Mock(return_value="0")
        api = SimpleNamespace(health=lambda *args: None, child_handles=lambda hwnd: iter([2, 3]) if hwnd == 1 else iter([]), metadata=lambda hwnd: {"hwnd": hwnd, "pid": 10, "visible": True, "class_name": "WindowsForms10.STATIC.app.0.test" if hwnd == 2 else "WindowsForms10.EDIT.app.0.test"}, read_label_text=reader)
        probe.check_native_controls(api, {"hwnd": 1, "pid": 10}, self.log, read_labels=True)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.args[0]["hwnd"], 2)
        self.assertEqual(next(e["text"] for e in probe.read_events(self.log) if e["event"] == "native_text"), "0")

    def test_text_timeout_retains_native_error_and_identity_without_label_contents(self):
        import ctypes
        data={'hwnd':90,'class_name':'WindowsForms10.STATIC.synthetic','visible':True}
        api=SimpleNamespace(user32=SimpleNamespace(SendMessageTimeoutW=mock.Mock(return_value=0)))
        with mock.patch.object(ctypes,'set_last_error',create=True) as clear, \
             mock.patch.object(ctypes,'get_last_error',return_value=1460,create=True):
            with self.assertRaises(NativeTextReadError) as caught:
                probe.NativeWindows.read_label_text(api,data,self.log)
        clear.assert_called_once_with(0)
        error=caught.exception
        self.assertEqual((error.winerror,error.hwnd,error.class_name),(1460,90,data['class_name']))
        self.assertEqual(probe.error_info(error)['error_code'],'NATIVE_TEXT_READ_FAILED')
        self.assertNotIn('text',probe.error_info(error))
        self.assertEqual(api.user32.SendMessageTimeoutW.call_args.args[5],100)

    def test_owner_combo_resolved_from_label_without_fixed_handles(self):
        events = [
            {"event": "native_text", "hwnd": 1, "class_name": "WindowsForms10.STATIC.app.0.test", "text": "货主", "rect": [224, 128, 253, 140]},
            {"event": "native_text", "hwnd": 2, "class_name": "WindowsForms10.COMBOBOX.app.0.test", "text": "测试货主", "rect": [283, 125, 589, 145]},
            {"event": "native_text", "hwnd": 3, "class_name": "WindowsForms10.COMBOBOX.app.0.test", "text": "", "rect": [734, 125, 899, 145]},
        ]
        self.assertEqual(probe.owner_combo_from_events(events)["hwnd"], 2)
        with self.assertRaises(WDMSError):
            probe.owner_combo_from_events(events[1:])

    def test_owner_combo_ambiguity_stops_reading(self):
        label = {"event": "native_text", "hwnd": 1, "class_name": "WindowsForms10.STATIC.app.0.test", "text": "货主", "rect": [0, 0, 30, 12]}
        combo = {"event": "native_text", "hwnd": 2, "class_name": "WindowsForms10.COMBOBOX.app.0.test", "text": "", "rect": [40, 0, 100, 12]}
        with self.assertRaises(WDMSError) as caught:
            probe.owner_combo_from_events([label, combo, {**combo, "hwnd": 3}])
        self.assertEqual(caught.exception.code, "OWNER_COMBO_AMBIGUOUS")

    def test_native_owner_list_uses_only_read_messages(self):
        import ctypes
        names = ["货主甲", "货主乙"]
        messages = []

        def send(hwnd, message, index, pointer, flags, timeout, result):
            messages.append(message)
            if message == 0x0146:
                result._obj.value = len(names)
            elif message == 0x0149:
                result._obj.value = len(names[index])
            elif message == 0x0148:
                text = ctypes.create_unicode_buffer(names[index])
                ctypes.memmove(pointer, text, ctypes.sizeof(text))
                result._obj.value = len(names[index])
            else:
                raise AssertionError("must not change selection or click")
            return 1

        api = object.__new__(probe.NativeWindows)
        api.user32 = SimpleNamespace(GetWindowLongW=lambda *args: 0, SendMessageTimeoutW=send)
        with mock.patch.object(probe.ctypes, "set_last_error", create=True):
            result = api.read_owner_list({"hwnd": 2, "class_name": "WindowsForms10.COMBOBOX.app.0.test"}, self.log)
        self.assertEqual(result, names)
        self.assertEqual(messages, [0x0146, 0x0149, 0x0148, 0x0149, 0x0148])

    def test_owner_drawn_nonstring_list_is_rejected_before_messages(self):
        api = object.__new__(probe.NativeWindows)
        sender = mock.Mock(side_effect=AssertionError("must not read item pointers"))
        api.user32 = SimpleNamespace(GetWindowLongW=lambda *args: 0x10, SendMessageTimeoutW=sender)
        with self.assertRaises(WDMSError):
            api.read_owner_list({"hwnd": 2, "class_name": "WindowsForms10.COMBOBOX.app.0.test"}, self.log)
        sender.assert_not_called()

    def query_events(self, whole="262,350", loose="1312325.54", total="32772080.54"):
        def text(hwnd, kind, value, rect):
            return {"event": "native_text", "hwnd": hwnd, "class_name": f"WindowsForms10.{kind}.app.0.test", "text": value, "rect": rect}
        events = [
            text(1, "STATIC", "货主", [0, 0, 20, 12]), text(2, "COMBOBOX", "测试货主", [25, 0, 80, 12]),
            text(3, "BUTTON", "查询(&F)", [200, 0, 240, 20]),
            text(4, "STATIC", "整件数：", [0, 100, 20, 112]), text(5, "STATIC", whole, [30, 100, 65, 112]),
            text(6, "STATIC", "散件数：", [80, 100, 100, 112]), text(7, "STATIC", loose, [110, 100, 160, 112]),
        ]
        if total is not None:
            events.extend([text(8, "STATIC", "在库数量合计：", [0, 80, 20, 92]), text(9, "STATIC", total, [30, 80, 100, 92])])
        return events

    def query_api(self, states, events=None):
        events = self.query_events() if events is None else events
        lookup = {e["hwnd"]: e for e in events}
        for event in events:
            probe.record(self.log, event.pop("event"), **event)
        sender = mock.Mock(return_value=True)
        def read_text(item, log):
            if not probe.text_read_allowed(item):
                raise WDMSError("TEXT_CONTROL_NOT_ALLOWED", "test-only")
            return item["text"]

        def cursor(pointer):
            pointer._obj.x, pointer._obj.y = 220, 10
            return True
        api = SimpleNamespace(metadata=lambda hwnd: {"pid": 10, "visible": True, "rect": lookup[hwnd]["rect"] if hwnd in lookup else [0, 0, 1000, 800], "class_name": lookup[hwnd]["class_name"] if hwnd in lookup else "WindowsForms10.Window.app.0.test"}, health=lambda *args: None,
            read_label_text=read_text, click_query_input=sender, user32=SimpleNamespace(IsWindowEnabled=mock.Mock(side_effect=states), IsWindow=lambda hwnd: True, PostMessageW=sender, GetParent=lambda hwnd: 42, GetDlgCtrlID=lambda hwnd: 3))
        api.user32.GetForegroundWindow = lambda: 10
        api.user32.GetCursorPos = cursor
        api.user32.WindowFromPoint = lambda point: 3
        enabled = api.user32.IsWindowEnabled
        api.user32.IsWindowEnabled = lambda hwnd: True if hwnd == 10 else enabled(hwnd)
        return api, sender

    def test_one_query_observes_busy_idle_and_keeps_decimal_snapshot(self):
        api, sender = self.query_api([True, False, True, True, True, True])
        with mock.patch.object(probe.time, "sleep"):
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=2)
        sender.assert_called_once_with({"hwnd": 10, "pid": 10}, 3, self.log)
        completed = next(e for e in probe.read_events(self.log) if e["event"] == "single_query_observation_complete")
        self.assertEqual(completed["whole"], "262350")
        self.assertEqual(completed["loose"], "1312325.54")
        self.assertFalse(completed["result_owner_independently_verified"])
        self.assertTrue(completed["requires_manual_review"])

    def test_dangling_decimal_point_on_total_does_not_block_query(self):
        events = self.query_events(whole="124,255", loose="659506", total="18585069.")
        api, sender = self.query_api([True, False, True, True, True, True], events)
        with mock.patch.object(probe.time, "sleep"):
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=2)
        sender.assert_called_once()
        completed = next(e for e in probe.read_events(self.log) if e["event"] == "single_query_observation_complete")
        self.assertEqual(completed["whole"], "124255")
        self.assertEqual(completed["loose"], "659506")
        self.assertEqual(completed["total"], "18585069")
        self.assertFalse(completed["result_owner_independently_verified"])

    def test_missing_total_controls_never_block_collection(self):
        api, sender = self.query_api([True, False, True, True, True, True], self.query_events(total=None))
        with mock.patch.object(probe.time, "sleep"):
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=2)
        sender.assert_called_once()
        completed = next(e for e in probe.read_events(self.log) if e["event"] == "single_query_observation_complete")
        self.assertIsNone(completed["total"])
        self.assertEqual(completed["loose"], "1312325.54")

    def test_no_busy_transition_is_inconclusive_and_never_retried(self):
        api, sender = self.query_api(None)
        api.user32.IsWindowEnabled = lambda hwnd: True
        with mock.patch.object(probe.time, "sleep"):
            with self.assertRaises(WDMSError) as caught:
                probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=0.01)
        self.assertEqual(caught.exception.code, "QUERY_TRANSITION_NOT_OBSERVED")
        self.assertEqual(sender.call_count, 1)

    def test_disabled_button_is_not_clicked(self):
        api, sender = self.query_api([False])
        with self.assertRaises(WDMSError) as caught:
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=1)
        self.assertEqual(caught.exception.code, "QUERY_ALREADY_RUNNING")
        sender.assert_not_called()

    def test_live_invisible_control_is_rejected_without_assuming_visibility(self):
        api, sender = self.query_api([True])
        original = api.metadata
        api.metadata = lambda hwnd: {**original(hwnd), "visible": False}
        with self.assertRaises(WDMSError) as caught:
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=1)
        self.assertEqual(caught.exception.code, "CONTROL_NOT_VISIBLE")
        sender.assert_not_called()

    def test_query_parent_from_other_process_is_not_notified(self):
        api, sender = self.query_api([True])
        original = api.metadata
        api.metadata = lambda hwnd: {**original(hwnd), "pid": 99 if hwnd == 42 else 10}
        with self.assertRaises(WDMSError) as caught:
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, timeout=1)
        self.assertEqual(caught.exception.code, "QUERY_PARENT_INVALID")
        sender.assert_not_called()

    def input_api(self, hit=3, sent=2):
        api = object.__new__(probe.NativeWindows)
        api.metadata = lambda hwnd: {"pid": 10, "visible": True, "rect": [200, 0, 240, 20]}
        def cursor(pointer):
            pointer._obj.x, pointer._obj.y = 220, 10
            return True
        sender = mock.Mock(return_value=sent)
        api.user32 = SimpleNamespace(SetForegroundWindow=lambda hwnd: True,
            GetForegroundWindow=lambda: 10, IsWindowEnabled=lambda hwnd: True,
            WindowFromPoint=lambda point: hit, GetAsyncKeyState=lambda key: 0,
            SetCursorPos=lambda x, y: True, GetCursorPos=cursor, SendInput=sender)
        return api, sender

    def test_mouse_input_queues_one_down_and_up(self):
        api, sender = self.input_api()
        api.click_query_input({"hwnd": 10, "pid": 10}, 3, self.log)
        sender.assert_called_once()
        count, inputs, size = sender.call_args.args
        self.assertEqual(count, 2)
        self.assertEqual([inputs[0].mi.dwFlags, inputs[1].mi.dwFlags], [2, 4])
        self.assertEqual([inputs[0].type, inputs[1].type], [0, 0])

    def test_obscured_button_receives_no_mouse_input(self):
        api, sender = self.input_api(hit=99)
        with self.assertRaises(WDMSError) as caught:
            api.click_query_input({"hwnd": 10, "pid": 10}, 3, self.log)
        self.assertEqual(caught.exception.code, "QUERY_BUTTON_OBSCURED")
        sender.assert_not_called()

    def test_partial_mouse_input_only_sends_release_without_retry(self):
        api, sender = self.input_api(sent=1)
        with self.assertRaises(WDMSError) as caught:
            api.click_query_input({"hwnd": 10, "pid": 10}, 3, self.log)
        self.assertEqual(caught.exception.code, "QUERY_INPUT_FAILED")
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(sender.call_args.args[0], 1)
        self.assertEqual(sender.call_args.args[1]._obj.mi.dwFlags, 4)

    def test_pressed_modifier_blocks_click(self):
        api, sender = self.input_api()
        api.user32.GetAsyncKeyState = lambda key: 0x8000 if key == 17 else 0
        with self.assertRaises(WDMSError) as caught:
            api.click_query_input({"hwnd": 10, "pid": 10}, 3, self.log)
        self.assertEqual(caught.exception.code, "QUERY_INPUT_BUSY")
        sender.assert_not_called()

    def test_alt_f_sends_balanced_keys_without_moving_mouse(self):
        api, sender = self.input_api(sent=4)
        api.user32.SetCursorPos = mock.Mock()
        api.query_shortcut_input({"hwnd": 10, "pid": 10}, 3, self.log)
        sender.assert_called_once()
        count, inputs, size = sender.call_args.args
        self.assertEqual(count, 4)
        self.assertEqual([(i.type, i.ki.wVk, i.ki.dwFlags) for i in inputs],
                         [(1, 18, 0), (1, 70, 0), (1, 70, 2), (1, 18, 2)])
        api.user32.SetCursorPos.assert_not_called()

    def test_partial_alt_f_only_releases_keys(self):
        api, sender = self.input_api(sent=2)
        with self.assertRaises(WDMSError):
            api.query_shortcut_input({"hwnd": 10, "pid": 10}, 3, self.log)
        self.assertEqual(sender.call_count, 2)
        count, releases, size = sender.call_args.args
        self.assertEqual(count, 2)
        self.assertEqual([(i.ki.wVk, i.ki.dwFlags) for i in releases], [(70, 2), (18, 2)])

    def test_alt_f_is_not_sent_to_other_foreground_window(self):
        api, sender = self.input_api(sent=4)
        api.user32.GetForegroundWindow = lambda: 99
        with mock.patch.object(probe.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(WDMSError) as caught:
                api.query_shortcut_input({"hwnd": 10, "pid": 10}, 3, self.log)
        self.assertEqual(caught.exception.code, "QUERY_FOREGROUND_FAILED")
        sender.assert_not_called()

    def test_alt_f_query_does_not_fall_back_to_mouse(self):
        api, mouse = self.query_api([True, False, True, True, True, True])
        api.query_shortcut_input = mock.Mock()
        with mock.patch.object(probe.time, "sleep"):
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, self.log, 2, trigger="alt-f")
        api.query_shortcut_input.assert_called_once()
        mouse.assert_not_called()

    def test_signal_observer_with_unchanged_values_does_not_claim_completion(self):
        api, mouse = self.query_api(None)
        api.user32.IsWindowEnabled = lambda hwnd: True
        api.user32.GetForegroundWindow = lambda: 10
        api.cursor_state = lambda: {"cursor_handle": 1, "standard_wait_cursor": False}
        api.query_shortcut_input = mock.Mock()
        with mock.patch.object(probe.time, "sleep"):
            probe.observe_query_signals(api, {"hwnd": 10, "pid": 10}, self.log, 0.002, "alt-f")
        api.query_shortcut_input.assert_called_once()
        mouse.assert_not_called()
        result = next(e for e in probe.read_events(self.log) if e["event"] == "query_signal_observation_complete")
        self.assertFalse(result["query_completion_verified"])
        self.assertFalse(result["values_changed"])

    def test_signal_observer_records_changed_values_without_claiming_completion(self):
        api, mouse = self.query_api(None)
        api.user32.IsWindowEnabled = lambda hwnd: True
        api.user32.GetForegroundWindow = lambda: 10
        api.cursor_state = lambda: {"cursor_handle": 1, "standard_wait_cursor": False}
        reading = api.read_label_text
        changed = [False]
        api.query_shortcut_input = lambda *args: changed.__setitem__(0, True)
        api.read_label_text = lambda item, log: "1302941.54" if changed[0] and item["hwnd"] == 7 else reading(item, log)
        with mock.patch.object(probe.time, "sleep"):
            probe.observe_query_signals(api, {"hwnd": 10, "pid": 10}, self.log, 0.002, "alt-f")
        result = next(e for e in probe.read_events(self.log) if e["event"] == "query_signal_observation_complete")
        self.assertTrue(result["values_changed"])
        self.assertFalse(result["query_completion_verified"])
        self.assertEqual(result["last_observed_values"]["loose"], "1302941.54")

    def settling_api(self):
        api, sender = self.query_api(None)
        api.user32.IsWindowEnabled = lambda hwnd: True
        api.user32.GetForegroundWindow = lambda: 10
        controls = probe.query_controls_from_events(probe.read_events(self.log))
        for data in controls.values():
            data["visible"] = True
        return api, controls

    def test_two_wait_phases_do_not_finish_at_first_idle_gap(self):
        api, controls = self.settling_api()
        clock = [0.0]
        api.cursor_state = lambda: {"standard_wait_cursor": clock[0] < 1 or 3 <= clock[0] < 4, "cursor_visible": True}
        with mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(probe.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            probe.wait_cursor_and_values(api, {"hwnd": 10, "pid": 10}, controls, "测试货主", self.log, deadline=20)
        self.assertGreaterEqual(clock[0], 9)
        self.assertLess(clock[0], 10)

    def test_late_value_change_restarts_settling_period(self):
        api, controls = self.settling_api()
        clock = [0.0]
        original = api.read_label_text
        api.cursor_state = lambda: {"standard_wait_cursor": clock[0] < 1, "cursor_visible": True}
        api.read_label_text = lambda item, log: "200" if item["hwnd"] == 7 and clock[0] >= 4 else original(item, log)
        with mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(probe.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            probe.wait_cursor_and_values(api, {"hwnd": 10, "pid": 10}, controls, "测试货主", self.log, deadline=20)
        self.assertGreaterEqual(clock[0], 9)

    def test_cursor_never_busy_cannot_confirm_query(self):
        api, controls = self.settling_api()
        clock = [0.0]
        api.cursor_state = lambda: {"standard_wait_cursor": False, "cursor_visible": True}
        with mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(probe.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(WDMSError) as caught:
                probe.wait_cursor_and_values(api, {"hwnd": 10, "pid": 10}, controls, "测试货主", self.log, deadline=10)
        self.assertEqual(caught.exception.code, "QUERY_CURSOR_NOT_SETTLED")

    def test_foreground_change_stops_cursor_completion_check(self):
        api, controls = self.settling_api()
        api.user32.GetForegroundWindow = lambda: 99
        with self.assertRaises(WDMSError) as caught:
            probe.wait_cursor_and_values(api, {"hwnd": 10, "pid": 10}, controls, "测试货主", self.log, deadline=probe.time.monotonic()+10)
        self.assertEqual(caught.exception.code, "QUERY_FOREGROUND_CHANGED")


if __name__ == "__main__":
    unittest.main()
