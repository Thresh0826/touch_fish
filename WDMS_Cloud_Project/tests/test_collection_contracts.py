"""Contract, deadline and I/O fault tests using only temporary synthetic data."""
import ast
import errno
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest import mock

from core import native_collection
from core.collection_contracts import CollectionJob, CollectionRequest, RunContext, WorkerJob
from core.native_collection import InventoryReading
from core.query_policy import QueryPolicy
from errors import NativeTextReadError, WDMSError
from services import native_results, native_worker, owner_catalog, trial_excel_reporter
from services.file_retry import retry_permission
from scripts import check_wdms_window as probe
from scripts import run_native_trial as trial
from native_fixtures import isolate_trial_notices, publish_completion
import test_window_probe as fixtures


class CollectionContractTests(unittest.TestCase):
    def setUp(self):
        isolate_trial_notices(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = tuple(f'合成契约货主{i}' for i in range(24))
        self.owners = self.root/'synthetic_plan.json'
        self.owners.write_text(json.dumps({'owners': self.names}), encoding='utf-8')
        unused, names, digest = owner_catalog.read_plan(self.owners)
        self.job = WorkerJob('worker-current', 'run-current', 0, self.owners, digest,
                             self.root/'owner.jsonl', self.root/'owner.result.json',
                             self.root/'owner.stderr.txt', 'synthetic-title', QueryPolicy(),
                             expected_owner=self.names[0])
        self.reading = InventoryReading(self.names[0], 1, Decimal('2.54'), 'loading...')

    def publish(self):
        native_results.write_worker_result(self.job, reading=self.reading, stage='synthetic')

    def test_receipt_preserves_counts_and_manual_review_evidence(self):
        self.publish()
        result = native_results.read_worker_result(self.job)
        self.assertEqual((result.whole, result.loose), (1, Decimal('2.54')))
        self.assertTrue(result.as_row()['requires_manual_review'])
        self.assertFalse(result.as_row()['result_owner_independently_verified'])

    def test_receipt_rejects_other_worker_run_index_or_plan(self):
        self.publish()
        for changes in ({'worker_id':'old-worker'}, {'run_id':'old-run'}, {'index':1}, {'owners_sha256':'0'*64}):
            with self.subTest(changes=changes), self.assertRaises(WDMSError):
                native_results.read_worker_result(replace(self.job, **changes))

    def test_receipt_rejects_wrong_owner(self):
        self.publish()
        with self.assertRaises(WDMSError):
            native_results.read_worker_result(replace(self.job, expected_owner=self.names[1]))

    def test_receipt_rejects_truncated_json(self):
        self.job.result.write_text('{"schema_version":', encoding='utf-8')
        with self.assertRaises(WDMSError) as caught:
            native_results.read_worker_result(self.job)
        self.assertEqual(caught.exception.code, 'TRIAL_RESULT_UNCONFIRMED')

    def test_receipt_rejects_boolean_schema_or_index(self):
        self.publish()
        original = json.loads(self.job.result.read_text(encoding='utf-8'))
        for field in ('schema_version', 'owner_index'):
            data = {**original, field: True}
            self.job.result.write_text(json.dumps(data), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(WDMSError):
                native_results.read_worker_result(self.job)

    def test_receipt_rejects_relaxed_evidence_flags(self):
        self.publish()
        data = json.loads(self.job.result.read_text(encoding='utf-8'))
        data['reading']['requires_manual_review'] = False
        self.job.result.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(WDMSError):
            native_results.read_worker_result(self.job)

    def test_failed_receipt_preserves_cause(self):
        native_results.write_worker_result(self.job, failure={'error_code':'QUERY_OWNER_MISMATCH','error':'synthetic mismatch'})
        with self.assertRaises(WDMSError) as caught:
            native_results.read_worker_result(self.job)
        self.assertEqual(caught.exception.code, 'QUERY_OWNER_MISMATCH')

    def test_receipt_cannot_overwrite_an_existing_result(self):
        self.publish()
        before = self.job.result.read_bytes()
        with self.assertRaises(WDMSError):
            native_results.write_worker_result(self.job, failure={'error_code':'X','error':'synthetic'})
        self.assertEqual(self.job.result.read_bytes(), before)

    def test_receipt_rejects_symlink(self):
        target = self.root/'target.json';target.write_text('{}', encoding='utf-8')
        try:
            self.job.result.symlink_to(target)
        except OSError as error:
            if getattr(error,'winerror',None)==1314 or error.errno in (errno.EPERM,errno.EACCES,errno.ENOTSUP):
                self.skipTest('当前环境不支持或未获准创建符号链接。')
            raise
        with self.assertRaises(WDMSError):
            native_results.read_worker_result(self.job)

    def test_transient_receipt_read_conflict_recovers(self):
        self.publish()
        original = Path.read_text
        calls = [0]
        def read(path, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 1: raise PermissionError('synthetic conflict')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', new=read), mock.patch('services.file_retry.time.sleep'):
            self.assertEqual(native_results.read_worker_result(self.job), self.reading)

    def test_transient_receipt_publish_conflict_recovers_without_query(self):
        original = Path.replace
        calls = [0]
        def publish(path, target):
            calls[0] += 1
            if calls[0] == 1: raise PermissionError('synthetic conflict')
            return original(path, target)
        with mock.patch.object(Path, 'replace', new=publish), mock.patch('services.file_retry.time.sleep'):
            self.publish()
        self.assertEqual(native_results.read_worker_result(self.job), self.reading)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_completion_log_alone_cannot_publish_a_report(self):
        def worker(command, log, timeout, **kwargs):
            probe.record(log, 'trial_owner_complete', owner=self.names[0], whole='1', loose='2.54')
            return 0
        with mock.patch.object(probe, 'supervise', side_effect=worker), mock.patch.object(trial, 'show_notice'), contextlib.redirect_stdout(io.StringIO()):
            code = trial.main(['--limit','1','--owners',str(self.owners),'--out',str(self.root/'out')])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.root.rglob('*.xlsx')), [])

    def test_receipt_can_complete_without_a_completion_log(self):
        def worker(command, log, timeout, **kwargs):
            publish_completion(command, self.names[0])
            return 0
        with mock.patch.object(probe, 'supervise', side_effect=worker), mock.patch.object(trial, 'show_notice'), contextlib.redirect_stdout(io.StringIO()):
            code = trial.main(['--limit','1','--owners',str(self.owners),'--out',str(self.root/'out')])
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.root.rglob('*.xlsx'))), 1)

    def test_completed_receipt_with_nonzero_exit_is_still_a_failure(self):
        def worker(command, log, timeout, **kwargs):
            publish_completion(command, self.names[0])
            return 7
        with mock.patch.object(probe, 'supervise', side_effect=worker), mock.patch.object(trial, 'show_notice'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trial.main(['--limit','1','--owners',str(self.owners),'--out',str(self.root/'out')]), 2)
        self.assertEqual(list(self.root.rglob('*.xlsx')), [])

    def test_boolean_worker_exit_is_not_an_integer_success(self):
        def worker(command, log, timeout, **kwargs):
            publish_completion(command, self.names[0])
            return False
        with mock.patch.object(probe,'supervise',side_effect=worker), mock.patch.object(trial,'show_notice'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trial.main(['--limit','1','--owners',str(self.owners),'--out',str(self.root/'out')]),2)
        self.assertEqual(list(self.root.rglob('*.xlsx')),[])

    def test_report_date_is_fixed_across_midnight(self):
        day = date(2026, 10, 13)
        request = CollectionRequest(1, self.root/'out', day, 'synthetic-midnight', owners=self.owners)
        def worker(command, log, timeout, **kwargs):
            publish_completion(command, self.names[0])
            return 0
        with mock.patch.object(trial, 'date') as tomorrow, mock.patch.object(trial_excel_reporter, 'date') as writer_date, \
                mock.patch.object(probe, 'supervise', side_effect=worker), \
                mock.patch.object(trial, 'show_notice'), contextlib.redirect_stdout(io.StringIO()):
            tomorrow.today.return_value = date(2026, 10, 14)
            writer_date.today.return_value = date(2026, 10, 14)
            self.assertEqual(trial.run_collection(request), 0)
        report = next(self.root.rglob('*.xlsx'))
        self.assertEqual(report.name, '2026年10月13日物流库存.xlsx')
        trial_excel_reporter.verify_report(report, [self.names[0]], day)

    def test_one_second_file_retry_is_bounded(self):
        clock = [0.0]
        calls = [0]
        def denied():
            calls[0] += 1
            raise PermissionError('synthetic permanent denial')
        with self.assertRaises(PermissionError):
            retry_permission(denied, clock=lambda:clock[0], sleep=lambda delay:clock.__setitem__(0,clock[0]+delay))
        self.assertEqual(clock[0], 1.0)
        self.assertLess(calls[0], 12)

    def test_file_retry_does_not_retry_other_errors(self):
        operation = mock.Mock(side_effect=OSError('synthetic disk error'))
        with self.assertRaises(OSError): retry_permission(operation)
        operation.assert_called_once()

    def test_invalid_retry_duration_is_rejected(self):
        for seconds in (True, float('nan'), float('inf'), -1):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                retry_permission(lambda:None, seconds=seconds)

    def test_policy_rejects_unbounded_waits(self):
        for changes in ({'idle_seconds':float('inf')}, {'query_seconds':float('nan')}, {'preparation_seconds':True}, {'sample_count':2}):
            with self.subTest(changes=changes), self.assertRaises(WDMSError): QueryPolicy(**changes)

    def test_public_contract_annotations_work_on_python39(self):
        for model in (RunContext, CollectionRequest, WorkerJob, CollectionJob, InventoryReading):
            self.assertTrue(get_type_hints(model))

    def test_core_does_not_import_ui_cli_or_win32(self):
        root = Path(__file__).resolve().parents[1]/'core'
        for name in ('native_collection.py','native_batch.py','native_workflow.py','collection_contracts.py','query_policy.py','report_contract.py'):
            for node in ast.walk(ast.parse((root/name).read_text(encoding='utf-8'))):
                modules = [node.module] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
                self.assertFalse(any(module and module.split('.')[0] in ('scripts','desktop_demo','ctypes','argparse','subprocess') for module in modules), name)

    def test_missing_openpyxl_does_not_prevent_loading_the_preflight(self):
        code = '''import builtins
original=builtins.__import__
def blocked(name,*args,**kwargs):
    if name == 'openpyxl' or name.startswith('openpyxl.'):
        raise ImportError('synthetic missing report dependency')
    return original(name,*args,**kwargs)
builtins.__import__=blocked
from desktop_demo import workflow
print('preflight-loaded')
'''
        result = subprocess.run([sys.executable,'-B','-c',code],cwd=str(Path(__file__).resolve().parents[1]),
                                capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('preflight-loaded',result.stdout)

    def test_missing_report_dependency_stops_collection_before_any_worker(self):
        import builtins
        original=builtins.__import__
        def blocked(name,globals=None,locals=None,fromlist=(),level=0):
            if name=='services' and 'trial_excel_reporter' in fromlist:
                raise ImportError('synthetic missing report dependency')
            return original(name,globals,locals,fromlist,level)
        with mock.patch.object(probe,'supervise') as worker, mock.patch.object(trial,'show_notice'), \
                mock.patch('builtins.__import__',side_effect=blocked), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trial.main(['--limit','1','--owners',str(self.owners),'--out',str(self.root/'out')]),2)
        worker.assert_not_called()
        log=next((self.root/'out').glob('native_trial_*'))/'trial_error.jsonl'
        self.assertEqual(probe.read_events(log)[-1]['error_code'],'WORKFLOW_REPORT_DEPENDENCY_MISSING')

    def test_invalid_reading_types_are_rejected(self):
        for values in ((True,Decimal('1')), (1,Decimal('NaN')), (1,-Decimal('1')), (1,1.5)):
            with self.subTest(values=values), self.assertRaises(WDMSError):
                InventoryReading(self.names[0],*values)

    def test_expired_worker_does_not_open_a_native_client(self):
        client = mock.Mock()
        with mock.patch('services.native_worker.time.monotonic',return_value=1):
            code = native_worker.run_owner_worker(replace(self.job,deadline=0),client)
        self.assertEqual(code,2)
        client.connect.assert_not_called()
        with self.assertRaises(WDMSError) as caught:native_results.read_worker_result(self.job)
        self.assertEqual(caught.exception.code,'STEP_TIMEOUT')

    def native_client(self, clock, sent=4):
        from adapters.native_windows import NativeTrialClient, NativeWindows
        fixture=fixtures.TestWindowProbe();fixture.setUp();self.addCleanup(fixture.doCleanups)
        api, unused=fixture.query_api(None)
        api.find_main=lambda *args:[{'hwnd':10,'pid':10,'minimized':False}]
        api.read_owner_list=lambda *args:list(self.names)
        reading=api.read_label_text
        api.read_label_text=lambda data,log:self.names[0] if data['hwnd']==2 else reading(data,log)
        api.user32.IsWindowEnabled=lambda hwnd:True
        api.user32.SetForegroundWindow=lambda hwnd:True
        api.user32.SetCursorPos=lambda x,y:True
        api.user32.GetAsyncKeyState=lambda key:0
        def combo(hwnd,message,wparam,lparam,flags,timeout,result):
            result._obj.value=0
            return True
        api.user32.SendMessageTimeoutW=combo
        busy_until=[1.0]
        api.cursor_state=lambda:{'standard_wait_cursor':clock[0]<busy_until[0],'cursor_visible':True}
        def send(count, inputs, size):
            busy_until[0]=clock[0]+1
            return sent if count==4 else count
        sender=mock.Mock(side_effect=send)
        api.user32.SendInput=sender
        api._guard_query_input=lambda *args:NativeWindows._guard_query_input(api,*args)
        api.query_shortcut_input=lambda *args:NativeWindows.query_shortcut_input(api,*args)
        client=NativeTrialClient(self.job.log,self.job.title,api_factory=lambda:api,
                                 scanner=lambda *args,**kwargs:fixture.query_events(),environment_check=lambda:None)
        return client,sender

    def test_native_worker_waits_then_sends_once_and_publishes_a_bound_receipt(self):
        clock=[0.0]
        client,sender=self.native_client(clock)
        with mock.patch('services.native_worker.time.monotonic',side_effect=lambda:clock[0]), \
                mock.patch('services.native_worker.time.sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)):
            self.assertEqual(native_worker.run_owner_worker(self.job,client),0)
        sender.assert_called_once()
        result=native_results.read_worker_result(self.job)
        self.assertEqual((result.owner,result.whole,result.loose),(self.names[0],262350,Decimal('1312325.54')))
        events=probe.read_events(self.job.log)
        self.assertTrue(any(event['event']=='query_idle_recovered' for event in events))
        self.assertTrue(any(event['event']=='trial_owner_complete' for event in events))

    def test_partial_native_worker_input_releases_and_publishes_failure(self):
        clock=[0.0]
        client,sender=self.native_client(clock,sent=2)
        with mock.patch('services.native_worker.time.monotonic',side_effect=lambda:clock[0]), \
                mock.patch('services.native_worker.time.sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)):
            self.assertEqual(native_worker.run_owner_worker(self.job,client),2)
        self.assertEqual(sender.call_count,2)
        self.assertEqual([item.ki.dwFlags for item in sender.call_args.args[1]],[2,2])
        with self.assertRaises(WDMSError) as caught:native_results.read_worker_result(self.job)
        self.assertEqual(caught.exception.code,'QUERY_INPUT_FAILED')


class NativeGuardContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name)/'events.jsonl'

    def input_api(self, sent=4):
        api, sender = fixtures.TestWindowProbe().input_api(sent=sent)
        api.cursor_state = lambda:{'standard_wait_cursor':False,'cursor_visible':True}
        return api, sender

    def test_partial_keys_are_released_before_a_post_input_log_failure(self):
        api, sender = self.input_api(sent=2)
        original = probe.record
        def record(path, event, **data):
            if event == 'keyboard_input_sent': raise OSError('synthetic log failure')
            original(path,event,**data)
        with mock.patch('adapters.native_windows.record', side_effect=record):
            with self.assertRaises(OSError): api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        self.assertEqual(sender.call_count,2)
        self.assertEqual([i.ki.dwFlags for i in sender.call_args.args[1]], [2,2])

    def test_partial_mouse_is_released_before_a_post_input_log_failure(self):
        api, sender = self.input_api(sent=1)
        original = probe.record
        def record(path,event,**data):
            if event == 'mouse_input_sent': raise OSError('synthetic log failure')
            original(path,event,**data)
        with mock.patch('adapters.native_windows.record', side_effect=record):
            with self.assertRaises(OSError): api.click_query_input({'hwnd':10,'pid':10},3,self.log)
        self.assertEqual(sender.call_count,2)
        self.assertEqual(sender.call_args.args[1]._obj.mi.dwFlags,4)

    def test_hidden_cursor_never_confirms_idle(self):
        session = SimpleNamespace(guard_cursor=lambda:None, cursor_state=lambda:{'standard_wait_cursor':False,'cursor_visible':False})
        clock = [0.0]
        with self.assertRaises(WDMSError):
            native_collection.wait_until_idle(session,'合成货主',1,emit=lambda *args,**kwargs:None,
                clock=lambda:clock[0],sleep=lambda delay:clock.__setitem__(0,clock[0]+delay))
        self.assertEqual(clock[0],1.0)

    def test_query_guard_cancellation_stops_before_input(self):
        api, sender = self.input_api()
        api.query_input_guard = mock.Mock(side_effect=WDMSError('TASK_CANCELLED','synthetic cancel'))
        with self.assertRaises(WDMSError): api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        sender.assert_not_called()

    def test_query_guard_rejects_busy_cursor_before_input(self):
        api, sender = self.input_api()
        api.query_input_guard = lambda:None
        api.cursor_state = lambda:{'standard_wait_cursor':True,'cursor_visible':True}
        with self.assertRaises(WDMSError): api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        sender.assert_not_called()

    def test_reused_main_handle_from_another_pid_cannot_receive_input(self):
        api,sender=self.input_api()
        original=api.metadata
        api.metadata=lambda hwnd:{**original(hwnd),'pid':99 if hwnd==10 else 10}
        with self.assertRaises(WDMSError):api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        sender.assert_not_called()

    def test_modal_disabled_main_cannot_receive_input(self):
        api,sender=self.input_api()
        api.user32.IsWindowEnabled=lambda hwnd:hwnd!=10
        with self.assertRaises(WDMSError):api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        sender.assert_not_called()

    def test_changed_count_control_during_sampling_cannot_complete(self):
        fixture=fixtures.TestWindowProbe();fixture.setUp();self.addCleanup(fixture.doCleanups)
        api,sender=fixture.query_api([True,False,True,True,True,True])
        sampling=[False]
        original=api.metadata
        api.metadata=lambda hwnd:{**original(hwnd),'pid':99 if sampling[0] and hwnd==5 else 10}
        api.health=lambda hwnd,log,stage:sampling.__setitem__(0,True) if stage=='after_single_query' else None
        with mock.patch.object(probe.time,'sleep'), self.assertRaises(WDMSError):
            probe.test_current_owner_query(api,{'hwnd':10,'pid':10},fixture.log,2)
        sender.assert_called_once()
        self.assertFalse(any(event['event']=='single_query_observation_complete' for event in probe.read_events(fixture.log)))

    def test_owner_name_is_rechecked_after_the_intent_log(self):
        from adapters.native_windows import NativeQuerySession
        api,sender=self.input_api()
        api.metadata=lambda hwnd:{'pid':10,'visible':True,'class_name':'OWNER' if hwnd==2 else 'BUTTON','rect':[200,0,240,20]}
        api.read_label_text=lambda *args:'另一个合成货主'
        session=NativeQuerySession(api,{'hwnd':10,'pid':10},{'owner':{'hwnd':2,'class_name':'OWNER'},'search':{'hwnd':3,'class_name':'BUTTON'}},self.log)
        session.prepare_input('本轮合成货主',lambda:None)
        with self.assertRaises(WDMSError) as caught:api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        self.assertEqual(caught.exception.code,'QUERY_OWNER_MISMATCH')
        sender.assert_not_called()

    def test_owner_index_is_rechecked_after_the_intent_log(self):
        from adapters.native_windows import NativeQuerySession
        api,sender=self.input_api()
        api.metadata=lambda hwnd:{'pid':10,'visible':True,'class_name':'OWNER' if hwnd==2 else 'BUTTON','rect':[200,0,240,20]}
        api.read_label_text=lambda *args:'本轮合成货主'
        def send(hwnd,message,wparam,lparam,flags,timeout,result):result._obj.value=0;return True
        api.user32.SendMessageTimeoutW=send
        session=NativeQuerySession(api,{'hwnd':10,'pid':10},{'owner':{'hwnd':2,'class_name':'OWNER'},'search':{'hwnd':3,'class_name':'BUTTON'}},self.log,expected_index=1)
        session.prepare_input('本轮合成货主',lambda:None)
        with self.assertRaises(WDMSError) as caught:api.query_shortcut_input({'hwnd':10,'pid':10},3,self.log)
        self.assertEqual(caught.exception.code,'QUERY_OWNER_MISMATCH')
        sender.assert_not_called()

    def test_scan_retries_only_read_only_timeout_and_uses_the_new_snapshot(self):
        from adapters.native_windows import NativeTrialClient
        fixture = fixtures.TestWindowProbe();fixture.setUp();self.addCleanup(fixture.doCleanups)
        events = fixture.query_events()
        failure = NativeTextReadError('NATIVE_TEXT_READ_FAILED','synthetic timeout',winerror=1460,hwnd=5,class_name='WindowsForms10.STATIC.synthetic')
        scanner = mock.Mock(side_effect=[failure,events])
        client = NativeTrialClient(self.log,'synthetic',scanner=scanner)
        client.context = SimpleNamespace(deadline=100,check=lambda:None)
        client.api = mock.Mock();client.main={'hwnd':10,'pid':10}
        with mock.patch('adapters.native_windows.time.monotonic',return_value=0), mock.patch('adapters.native_windows.time.sleep'):
            client.locate_controls(25)
        self.assertEqual(scanner.call_count,2)
        self.assertEqual(client.controls['search']['hwnd'],3)
        client.api.query_shortcut_input.assert_not_called()

    def test_scan_access_denied_is_not_retried(self):
        from adapters.native_windows import NativeTrialClient
        failure = NativeTextReadError('NATIVE_TEXT_READ_FAILED','synthetic denied',winerror=5,hwnd=5,class_name='WindowsForms10.STATIC.synthetic')
        scanner = mock.Mock(side_effect=failure)
        client = NativeTrialClient(self.log,'synthetic',scanner=scanner)
        client.context=SimpleNamespace(deadline=100,check=lambda:None)
        client.api=mock.Mock();client.main={'hwnd':10,'pid':10}
        with mock.patch('adapters.native_windows.time.monotonic',return_value=0), self.assertRaises(WDMSError): client.locate_controls(25)
        scanner.assert_called_once()
