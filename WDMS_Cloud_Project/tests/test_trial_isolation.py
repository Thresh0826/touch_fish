"""Keep synthetic collection tests silent and verify the GUI workflow route."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_demo import notices
import test_collection_contracts as contract_tests
import test_native_robustness as robustness_tests
import test_native_trial as trial_tests


class SyntheticNoticeIsolationTests(unittest.TestCase):
    def check_windows_scenarios(self, cases):
        library = mock.Mock()
        library.MessageBoxW.return_value = 1
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def spawn(command, **kwargs):
            # A leaked notice can finish safely while the regression records it.
            Path(command[command.index('--notice-ready')+1]).write_text('ready', encoding='utf-8')
            return process

        console = io.StringIO()
        with mock.patch.object(notices.sys, 'platform', 'win32'), \
                mock.patch.object(notices.ctypes, 'WinDLL', return_value=library, create=True) as dll, \
                mock.patch.object(notices.subprocess, 'Popen', side_effect=spawn) as start, \
                contextlib.redirect_stdout(console):
            suite = unittest.TestSuite(case(name) for case, name in cases)
            result = unittest.TextTestRunner(stream=console).run(suite)
        self.assertTrue(result.wasSuccessful(), console.getvalue())
        start.assert_not_called()
        dll.assert_not_called()

    def test_expected_first_owner_failure_does_not_open_windows_notices(self):
        self.check_windows_scenarios([
            (trial_tests.NativeTrialTests, 'test_first_owner_failure_stops_batch_without_excel'),
        ])

    def test_success_deadline_and_budget_scenarios_do_not_spawn_windows_notices(self):
        self.check_windows_scenarios([
            (trial_tests.NativeTrialTests, 'test_default_collection_reads_names_without_source_json_or_owner_dialog'),
            (trial_tests.NativeTrialTests, 'test_total_deadline_expiry_stops_without_manifest'),
            (contract_tests.CollectionContractTests, 'test_report_date_is_fixed_across_midnight'),
            (robustness_tests.NativeRobustnessTests, 'test_worker_budget_includes_preparation_idle_and_query'),
        ])


class DemoWorkflowRouteTests(unittest.TestCase):
    def check_route(self, frozen, scope, limit):
        try:
            from desktop_demo import app as demo
        except ImportError as error:
            if getattr(error, 'name', None) in ('tkinter', '_tkinter'):
                self.skipTest('当前Python没有Tk，跳过依赖GUI的按钮检查；采集隔离检查仍运行。')
            raise
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory)/'settings.json'
            controller = object.__new__(demo.App)
            controller.root = mock.Mock()
            controller.scope = mock.Mock()
            controller.scope.get.return_value = scope
            controller.process = None
            controller.output_handle = None
            with mock.patch.object(demo, 'settings_path', return_value=settings), \
                    mock.patch.object(demo.sys, 'frozen', frozen, create=True), \
                    mock.patch.object(demo.subprocess, 'Popen') as start:
                try:
                    controller.full_workflow()
                    prefix = [sys.executable] if frozen else [
                        sys.executable, str(demo.RESOURCE_ROOT/'scripts'/'WDMS_Demo.py')]
                    start.assert_called_once()
                    self.assertEqual(start.call_args.args[0], prefix+[
                        '--native-workflow', '--out', str(settings.parent/'workflow_logs'),
                        '--limit', str(limit)])
                    self.assertEqual(start.call_args.kwargs['stdin'], subprocess.DEVNULL)
                    self.assertIs(start.call_args.kwargs['stdout'], controller.output_handle)
                    self.assertEqual(start.call_args.kwargs['stderr'], subprocess.STDOUT)
                    controller.root.withdraw.assert_called_once()
                    controller.root.after.assert_called_once_with(300, controller.poll_login)
                finally:
                    if controller.output_handle is not None:
                        controller.output_handle.close()

    def test_source_one_click_routes_to_workflow_with_three_owners(self):
        self.check_route(False, '前 3 个货主', 3)

    def test_frozen_one_click_routes_to_workflow_with_twenty_four_owners(self):
        self.check_route(True, '全部 24 个货主', 24)
