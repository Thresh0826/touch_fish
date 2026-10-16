import tempfile
import unittest
from pathlib import Path
from unittest import mock

from errors import WDMSError
from scripts import run_startup_menu_diagnostic as flow


class StartupMenuTests(unittest.TestCase):
    def run_flow(self, login_code=0, login_verified=True, menu_code=0, menu_verified=True,
                 cancel_after_login=False):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            cancel = folder / "cancel.request"
            calls = []

            def login(args):
                calls.append("login")
                out = Path(args[args.index("--out") + 1])
                out.mkdir()
                if login_verified is not None:
                    flow.probe.record(out / "login.jsonl", "automatic_login_complete",
                                      login_success_verified=login_verified)
                if cancel_after_login:
                    cancel.touch()
                return login_code

            def menu(args):
                calls.append("menu")
                out = Path(args[args.index("--out") + 1]) / "menu_current"
                out.mkdir(parents=True)
                if menu_verified:
                    flow.probe.record(out / "diagnostic.jsonl", "menu_diagnostic_complete",
                                      read_only=True, click_performed=False, query_performed=False)
                return menu_code

            with mock.patch.object(flow.login, "main", side_effect=login), \
                    mock.patch.object(flow.menu, "main", side_effect=menu):
                try:
                    flow.run_stages(folder, cancel)
                    error = None
                except WDMSError as caught:
                    error = caught.code
            return calls, error, flow.probe.read_events(folder / "startup_menu.jsonl")

    def test_login_then_menu_with_verified_success(self):
        calls, error, events = self.run_flow()
        self.assertEqual(calls, ["login", "menu"])
        self.assertIsNone(error)
        self.assertEqual(events[-1]["event"], "startup_menu_complete")
        self.assertIs(events[-1]["menu_navigation_performed"], False)

    def test_login_failure_stops_menu_even_with_success_marker(self):
        calls, error, _ = self.run_flow(login_code=2)
        self.assertEqual(calls, ["login"])
        self.assertEqual(error, "STARTUP_MENU_LOGIN_FAILED")

    def test_unconfirmed_login_stops_menu(self):
        for verified in (None, False, 1):
            calls, error, _ = self.run_flow(login_verified=verified)
            self.assertEqual(calls, ["login"])
            self.assertEqual(error, "STARTUP_MENU_LOGIN_UNCONFIRMED")

    def test_cancellation_between_stages_stops_menu(self):
        calls, error, _ = self.run_flow(cancel_after_login=True)
        self.assertEqual(calls, ["login"])
        self.assertEqual(error, "TASK_CANCELLED")

    def test_menu_failure_or_missing_confirmation_never_completes(self):
        for options in ({"menu_code": 2}, {"menu_verified": False}):
            calls, error, events = self.run_flow(**options)
            self.assertEqual(calls, ["login", "menu"])
            self.assertIsNotNone(error)
            self.assertNotIn("startup_menu_complete", [event["event"] for event in events])

    def test_non_windows_never_calls_login(self):
        with mock.patch.object(flow.sys, "platform", "darwin"), \
                mock.patch.object(flow.login, "main") as login:
            self.assertEqual(flow.main([]), 2)
            login.assert_not_called()
