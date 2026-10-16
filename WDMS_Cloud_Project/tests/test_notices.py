"""Exercise the real Windows notice branch without launching WDMS or Win32 UI."""
import builtins
import contextlib
import io
import symtable
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_demo import notices, workflow
from services import event_log


class NoticeWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.notice = notices.RunningNotice(self.folder)

    def ready_process(self, command, **kwargs):
        self.notice.ready.write_text('ready', encoding='utf-8')
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        return process

    def test_windows_start_uses_the_source_resource_root(self):
        with mock.patch.object(notices.sys, 'platform', 'win32'), \
                mock.patch.object(notices.subprocess, 'Popen', side_effect=self.ready_process) as spawn:
            self.notice.start()
            self.notice.close()
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs['cwd'], str(Path(notices.__file__).resolve().parents[1]))
        self.assertIn('--_notice', spawn.call_args.args[0])
        self.assertTrue(self.notice.stop.exists())
        self.assertIsNone(self.notice.process)

    def test_frozen_notice_uses_bundle_directory_and_router(self):
        bundle = self.folder/'bundle'
        bundle.mkdir()
        with mock.patch.object(notices.sys, 'platform', 'win32'), \
                mock.patch.object(notices.sys, 'frozen', True, create=True), \
                mock.patch.object(notices.sys, '_MEIPASS', str(bundle), create=True), \
                mock.patch.object(notices.subprocess, 'Popen', side_effect=self.ready_process) as spawn:
            self.notice.start()
            self.notice.close()
        self.assertEqual(spawn.call_args.kwargs['cwd'], str(bundle))
        self.assertEqual(spawn.call_args.args[0][:3], [sys.executable, '--native-trial', '--_notice'])

    def test_failed_notice_spawn_remains_a_passive_warning(self):
        with mock.patch.object(notices.sys, 'platform', 'win32'), \
                mock.patch.object(notices.subprocess, 'Popen', side_effect=OSError('synthetic denial')), \
                contextlib.redirect_stdout(io.StringIO()) as console:
            self.notice.start()
        self.assertIsNone(self.notice.process)
        self.assertIn('运行提示窗未能显示', console.getvalue())

    def test_unready_notice_is_stopped_after_its_three_second_budget(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        clock = [0.0]
        with mock.patch.object(notices.sys, 'platform', 'win32'), \
                mock.patch.object(notices.subprocess, 'Popen', return_value=process), \
                mock.patch.object(notices.time, 'monotonic', side_effect=lambda:clock[0]), \
                mock.patch.object(notices.time, 'sleep', side_effect=lambda delay:clock.__setitem__(0, clock[0]+delay)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.notice.start()
        self.assertLess(clock[0], 3.1)
        process.wait.assert_called_once_with(timeout=1)
        self.assertTrue(self.notice.stop.exists())
        self.assertIsNone(self.notice.process)

    def test_real_synthetic_process_obeys_ready_and_stop_protocol(self):
        worker = '''import sys,time
from pathlib import Path
ready,stop=map(Path,sys.argv[1:])
ready.write_text('ready',encoding='utf-8')
while not stop.exists():time.sleep(.01)
'''
        command = [sys.executable, '-B', '-c', worker, str(self.notice.ready), str(self.notice.stop)]
        with mock.patch.object(notices.sys, 'platform', 'win32'), \
                mock.patch.object(notices, 'trial_command', return_value=command):
            try:
                self.notice.start()
                process = self.notice.process
                self.assertIsNotNone(process)
                self.assertTrue(self.notice.ready.exists())
            finally:
                self.notice.close()
        self.assertEqual(process.returncode, 0)

    def test_refactored_modules_have_no_unbound_global_reads(self):
        root = Path(__file__).resolve().parents[1]
        paths = ['desktop_demo/notices.py', 'adapters/native_windows.py',
                 'core/native_batch.py', 'core/native_collection.py', 'core/native_workflow.py',
                 'services/native_results.py', 'services/native_worker.py',
                 'services/process_supervisor.py', 'services/workflow_evidence.py', 'services/worker_commands.py']
        runtime_names = set(dir(builtins)) | {'__file__', '__name__', '__package__', '__loader__', '__spec__'}
        problems = []
        for name in paths:
            table = symtable.symtable((root/name).read_text(encoding='utf-8'), name, 'exec')
            defined = {symbol.get_name() for symbol in table.get_symbols()
                       if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}
            def check(scope):
                for symbol in scope.get_symbols():
                    if symbol.is_global() and symbol.is_referenced() and symbol.get_name() not in defined | runtime_names:
                        problems.append((name,scope.get_name(),symbol.get_name()))
                for child in scope.get_children():check(child)
            check(table)
        self.assertEqual(problems, [])

    def test_unknown_error_message_keeps_type_without_raw_sensitive_text(self):
        try:
            raise NameError('synthetic-sensitive-value')
        except NameError as error:
            info = event_log.error_info(error)
        self.assertIn('NameError', info.get('error', ''))
        self.assertNotIn('synthetic-sensitive-value', str(info))


class WorkflowNoticeStartupTests(unittest.TestCase):
    def test_real_notice_start_allows_workflow_to_reach_stage_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            settings=folder/'settings.json'
            report=folder/'synthetic-report.xlsx'
            process=mock.Mock()
            process.wait.return_value=0
            process.poll.return_value=None
            def spawn(command,**kwargs):
                Path(command[command.index('--notice-ready')+1]).write_text('ready',encoding='utf-8')
                return process
            with mock.patch.object(notices.sys,'platform','win32'), \
                    mock.patch.object(notices.subprocess,'Popen',side_effect=spawn), \
                    mock.patch.object(workflow,'settings_path',return_value=settings), \
                    mock.patch.object(workflow,'load_settings',return_value=object()), \
                    mock.patch.object(workflow,'preflight',return_value=workflow.WorkflowInputs(folder,None)), \
                    mock.patch.object(workflow,'run_stages',return_value=report) as stages, \
                    mock.patch.object(workflow.trial,'show_notice'):
                code=workflow.main(['--out',str(folder/'flows'),'--limit','3'])
            self.assertEqual(code,0)
            stages.assert_called_once()
