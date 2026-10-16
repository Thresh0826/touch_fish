import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_demo import login, navigation
from scripts import check_wdms_window as probe


class StepSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def process(self, *, poll=None, wait=None):
        item = mock.Mock()
        item.pid = 321
        item.poll.return_value = poll
        if wait is not None:
            item.wait.side_effect = wait
        return item

    def test_cancel_before_start_does_not_spawn(self):
        log = self.root / "cancel.jsonl"
        with mock.patch.object(probe.subprocess, "Popen") as popen:
            code = probe.supervise(["worker"], log, 5, cancelled=lambda: True)
        self.assertEqual(code, 2)
        popen.assert_not_called()
        event = probe.read_events(log)[-1]
        self.assertEqual((event["event"], event["error_code"]), ("probe_interrupted", "TASK_CANCELLED"))

    def test_running_cancel_terminates_only_started_child(self):
        log = self.root / "running-cancel.jsonl"
        process = self.process(poll=None, wait=[0])
        cancel = mock.Mock(side_effect=[False, True])
        with mock.patch.object(probe.subprocess, "Popen", return_value=process) as popen:
            code = probe.supervise(["worker"], log, 5, cancelled=cancel)
        self.assertEqual(code, 2)
        popen.assert_called_once()
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertEqual(probe.read_events(log)[-1]["error_code"], "TASK_CANCELLED")

    def test_short_wait_timeout_continues_until_child_exits(self):
        log = self.root / "short-wait.jsonl"
        clock = [0.0]

        def wait(timeout):
            if len(process.wait.call_args_list) == 1:
                clock[0] += 0.1
                raise subprocess.TimeoutExpired("worker", timeout)
            return 7

        process = self.process(poll=7, wait=wait)
        with mock.patch.object(probe.subprocess, "Popen", return_value=process), \
             mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]):
            code = probe.supervise(["worker"], log, 1, cancelled=lambda: False)
        self.assertEqual(code, 7)
        self.assertEqual([call.kwargs["timeout"] for call in process.wait.call_args_list], [0.25, 0.25])

    def test_overall_deadline_exhaustion_is_hard_timeout(self):
        log = self.root / "deadline.jsonl"
        clock = [0.0]
        cleanup = [0]

        def wait(timeout):
            if timeout == 0.5:
                cleanup[0] += 1
                if cleanup[0] == 1:
                    raise subprocess.TimeoutExpired("worker", timeout)
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("worker", timeout)

        process = self.process(poll=None, wait=wait)
        with mock.patch.object(probe.subprocess, "Popen", return_value=process), \
             mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]):
            code = probe.supervise(["worker"], log, 1, cancelled=lambda: False)
        self.assertEqual(code, 2)
        self.assertGreaterEqual(process.wait.call_count, 4)
        self.assertEqual(probe.read_events(log)[-1]["error_code"], "PROBE_HARD_TIMEOUT")
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()

    def test_normal_exit_code_is_preserved(self):
        log = self.root / "normal.jsonl"
        process = self.process(poll=23, wait=[23])
        with mock.patch.object(probe.subprocess, "Popen", return_value=process):
            code = probe.supervise(["worker"], log, 5, cancelled=lambda: False)
        self.assertEqual(code, 23)
        self.assertFalse(any(event.get("error_code") for event in probe.read_events(log)))

    def test_terminate_timeout_escalates_to_kill(self):
        log = self.root / "kill.jsonl"
        process = self.process(poll=None, wait=[subprocess.TimeoutExpired("worker", 5),
                                               subprocess.TimeoutExpired("worker", 0.5), 0])
        with mock.patch.object(probe.subprocess, "Popen", return_value=process):
            code = probe.supervise(["worker"], log, 5)
        self.assertEqual(code, 2)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list[-2].kwargs["timeout"], 0.5)

    def test_without_cancel_callback_keeps_one_wait_call(self):
        log = self.root / "legacy-wait.jsonl"
        process = self.process(poll=4, wait=[4])
        with mock.patch.object(probe.subprocess, "Popen", return_value=process):
            code = probe.supervise(["worker"], log, 9)
        self.assertEqual(code, 4)
        process.wait.assert_called_once_with(timeout=9.0)

    def test_invalid_timeout_never_starts_process(self):
        for timeout in (math.nan, math.inf, 0, -1):
            with self.subTest(timeout=timeout):
                log = self.root / "invalid.jsonl"
                with mock.patch.object(probe.subprocess, "Popen") as popen:
                    code = probe.supervise(["worker"], log, timeout)
                self.assertEqual(code, 2)
                popen.assert_not_called()
                self.assertEqual(probe.read_events(log)[-1]["error_code"], "PROBE_HARD_TIMEOUT")

    def test_login_and_navigation_supervision_and_navigation_diagnostic_cancellation(self):
        cancel_file = self.root / "cancel.request"
        cancel_file.touch()
        login_out = self.root / "login"
        navigation_out = self.root / "navigation"
        login_out.mkdir()
        navigation_out.mkdir()

        with mock.patch.object(login.probe, "supervise", return_value=2) as login_supervise, \
             mock.patch.object(login.probe, "read_events", return_value=[]), \
             mock.patch.object(login.time, "monotonic", return_value=100):
            login.main([
                "--out", str(login_out), "--cancel-file", str(cancel_file),
                "--execution-deadline", "115",
            ])
        login_args = login_supervise.call_args
        self.assertEqual(login_args.args[2], 15)
        self.assertTrue(login_args.kwargs["cancelled"]())
        login_command=login_args.args[0]
        self.assertEqual(login_command[login_command.index('--cancel-file')+1],str(cancel_file.resolve()))
        self.assertEqual(float(login_command[login_command.index('--execution-deadline')+1]),115.0)

        with mock.patch.object(navigation.probe, "supervise", return_value=2) as navigation_supervise, \
             mock.patch.object(navigation.time, "monotonic", return_value=100):
            navigation.main([
                "--out", str(navigation_out), "--cancel-file", str(cancel_file),
                "--execution-deadline", "150",
            ])
        navigation_args = navigation_supervise.call_args
        self.assertEqual(navigation_args.args[2], 50)
        self.assertTrue(navigation_args.kwargs["cancelled"]())
        self.assertEqual(navigation_args.args[0][-2:], ["--cancel-file", str(cancel_file.resolve())])
        self.assertNotIn("--execution-deadline", navigation_args.args[0])


if __name__ == "__main__":
    unittest.main()
