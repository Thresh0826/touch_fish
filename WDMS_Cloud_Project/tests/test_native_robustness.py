"""Failure scenarios at the native collection boundaries; all data is synthetic."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_window_probe as fixtures
from errors import WDMSError
from scripts import check_wdms_window as probe
from scripts import run_native_trial as trial
from native_fixtures import isolate_trial_notices


class NativeRobustnessTests(unittest.TestCase):
    def setUp(self):
        isolate_trial_notices(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "events.jsonl"

    def one_sharing_conflict(self):
        original = Path.open
        calls = [0]

        def open_file(path, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise PermissionError("synthetic sharing conflict")
            return original(path, *args, **kwargs)

        return open_file

    def input_api(self):
        fixture = fixtures.TestWindowProbe()
        api, sender = fixture.input_api()
        api.cursor_state = lambda: {"standard_wait_cursor": False, "cursor_visible": True}
        return api, sender

    def test_log_retry_does_not_send_keys_after_foreground_changes(self):
        api, sender = self.input_api()
        sender.return_value = 4
        foreground = [10]
        api.user32.GetForegroundWindow = lambda: foreground[0]
        with mock.patch.object(Path, "open", new=self.one_sharing_conflict()), \
                mock.patch.object(probe.time, "sleep", side_effect=lambda seconds: foreground.__setitem__(0, 99)):
            with self.assertRaises(WDMSError):
                api.query_shortcut_input({"hwnd": 10, "pid": 10}, 3, self.log)
        sender.assert_not_called()

    def test_log_retry_does_not_click_a_new_window_at_the_same_point(self):
        api, sender = self.input_api()
        hit = [3]
        api.user32.WindowFromPoint = lambda point: hit[0]
        with mock.patch.object(Path, "open", new=self.one_sharing_conflict()), \
                mock.patch.object(probe.time, "sleep", side_effect=lambda seconds: hit.__setitem__(0, 99)):
            with self.assertRaises(WDMSError):
                api.click_query_input({"hwnd": 10, "pid": 10}, 3, self.log)
        sender.assert_not_called()

    def test_normal_cursor_from_another_app_is_not_idle_evidence(self):
        foreground = [10]

        def guard():
            if foreground[0] != 10:
                raise WDMSError("QUERY_FOREGROUND_CHANGED", "synthetic foreground loss")

        session = SimpleNamespace(
            guard_cursor=guard,
            cursor_state=lambda: {"standard_wait_cursor": foreground[0] == 10, "cursor_visible": True},
        )
        with mock.patch.object(trial.time, "sleep", side_effect=lambda seconds: foreground.__setitem__(0, 99)):
            with self.assertRaises(WDMSError):
                trial.wait_until_idle(session, self.log, "合成货主")

    def test_total_grouping_changes_do_not_invalidate_the_two_counts(self):
        fixture = fixtures.TestWindowProbe()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        api, unused = fixture.query_api(None)
        api.user32.IsWindowEnabled = lambda hwnd: True
        api.user32.GetForegroundWindow = lambda: 10
        api.query_shortcut_input = mock.Mock()
        clock = [0.0]
        sampling = [False]
        totals = iter(["1,000", "1000", "1,000"])
        api.cursor_state = lambda: {"standard_wait_cursor": clock[0] < 1, "cursor_visible": True}
        api.health = lambda hwnd, log, stage: sampling.__setitem__(0, True) if stage == "after_single_query" else None
        original = api.read_label_text
        api.read_label_text = lambda data, log: (next(totals) if sampling[0] else "1000") if data["hwnd"] == 9 else original(data, log)
        with mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(probe.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            probe.test_current_owner_query(api, {"hwnd": 10, "pid": 10}, fixture.log, 20, "alt-f", "cursor")
        api.query_shortcut_input.assert_called_once()
        self.assertTrue(any(e["event"] == "single_query_observation_complete" for e in probe.read_events(fixture.log)))

    def test_worker_budget_includes_preparation_idle_and_query(self):
        owners = self.root / "owners.json"
        owners.write_text(json.dumps({"owners": [f"合成货主{i}" for i in range(24)]}), encoding="utf-8")
        with mock.patch.object(trial.probe, "supervise", return_value=2) as supervise, \
                mock.patch.object(trial, "show_notice"), contextlib.redirect_stdout(io.StringIO()):
            trial.main(["--limit", "1", "--owners", str(owners), "--out", str(self.root / "out")])
        self.assertGreaterEqual(supervise.call_args.args[2], 40 + 20 + 90)
