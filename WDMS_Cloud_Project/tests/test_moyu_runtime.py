from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from errors import WDMSError
from moyu_tools.engine import Engine
from moyu_tools.model import Plan, Policy, digest
from moyu_tools.runtime import adopt_binding, make_request, rearm, run_child, verify_child, worker
from moyu_tools.settings import load_json, validate_profile
from moyu_tools.store import Store
from services.status_manager import atomic_json_write
from tests.moyu_fixtures import Clock, Query, Remote, profile, zip_bytes


class MoyuRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.output = self.base/'output'
        self.output.mkdir()
        self.env = patch.dict('os.environ', {'LOCALAPPDATA': str(self.base/'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {'plan': Plan(date(2025, 1, 1), date(2025, 1, 7)).document(),
                       'policy': asdict(Policy(initial_wait=1, settle=.5, query_timeout=8,
                                             file_settle=.2, poll=.1)), 'output': str(self.output), 'task': ''}
        self.path, self.req = make_request(self.config, profile(), trial=True)

    def test_request_uses_unique_result_and_cancel_per_attempt(self):
        self.config['task'] = self.req['task']
        _, second = make_request(self.config, profile(), trial=True)
        self.assertEqual(self.req['task'], second['task'])
        self.assertNotEqual(self.req['cancel'], second['cancel'])
        self.assertNotEqual(self.req['result'], second['result'])

    def test_actual_worker_subprocess_on_non_windows_does_not_load_gui_or_native(self):
        with patch('moyu_tools.runtime.sys.platform', 'darwin'):
            self.assertEqual(worker(self.path), 2)
        with self.assertRaises(WDMSError) as caught:
            verify_child(2, self.req)
        self.assertEqual(caught.exception.code, 'MOYU_WINDOWS_REQUIRED')

    def test_real_controller_runs_non_windows_worker_and_reports_its_failure(self):
        import sys
        if sys.platform == 'win32':
            self.skipTest('Non-Windows boundary is tested directly in the preceding test')
        with self.assertRaises(WDMSError) as caught:
            run_child(self.path, self.req)
        self.assertEqual(caught.exception.code, 'MOYU_WINDOWS_REQUIRED')
        self.assertTrue(Path(self.req['result']).is_file())

    def test_deliberate_stop_before_launch_is_not_reported_as_success_or_failure(self):
        Path(self.req['cancel']).touch()
        self.assertEqual(run_child(self.path, self.req), 'stopped')
        self.assertFalse(Path(self.req['result']).exists())

    def test_real_parent_watchdog_terminates_blocked_stage_before_batch_timeout(self):
        self.config['policy']['preparation']=.15
        path,request=make_request(self.config,profile(),trial=True)
        fake=self.base/'blocked_worker.py'
        fake.write_text("import json,sys,time\nfrom pathlib import Path\nr=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
                        "Path(r['watch']).write_text(json.dumps({'request_id':r['request_id'],'token':'one-stage','seconds':r['policy']['preparation']}),encoding='utf-8')\n"
                        "time.sleep(30)\n",encoding='utf-8')
        started=time.monotonic()
        with patch('moyu_tools.runtime.ENTRY',fake):
            with self.assertRaises(WDMSError) as caught:
                run_child(path,request)
        self.assertEqual(caught.exception.code,'MOYU_STAGE_HARD_TIMEOUT')
        self.assertLess(time.monotonic()-started,5)
        self.assertTrue(Path(request['watch']).exists())
        self.assertFalse(Path(request['result']).exists())

    def test_stale_stage_watch_cannot_extend_current_request(self):
        atomic_json_write(Path(self.req['watch']),{'request_id':'old','token':'old','seconds':600})
        with self.assertRaises(WDMSError) as caught:
            run_child(self.path,self.req)
        self.assertEqual(caught.exception.code,'MOYU_WATCH_INVALID')

    def test_windows_worker_route_uses_actual_engine_files_and_receipts_without_gui(self):
        clock = Clock()
        query, remote = Query(clock), Remote()
        parent = Mock()
        parent.alive.return_value = True
        def create_engine(plan, policy, store, q, r, **kwargs):
            return Engine(plan, policy, store, q, r, clock=clock, sleep=clock.sleep, wall=clock, **kwargs)
        with patch('moyu_tools.runtime.sys.platform', 'win32'), \
             patch('moyu_tools.bridge.Bridge') as bridge, \
             patch('moyu_tools.bridge.ParentHandle', return_value=parent), \
             patch('moyu_tools.native.Driver'), \
             patch('moyu_tools.native.LocalQuery', return_value=query), \
             patch('moyu_tools.native.RemoteFiles', return_value=remote), \
             patch('moyu_tools.runtime.Engine', side_effect=create_engine):
            self.assertEqual(worker(self.path), 0)
            bridge.return_value.close.assert_called_once()
        self.assertEqual(verify_child(0, self.req), 'trial_passed')
        self.assertEqual(remote.searches, ['001234'])
        self.assertTrue((self.output/remote.name).exists())
        parent.close.assert_called_once()

    def test_controller_death_cancels_before_native_inputs(self):
        clock = Clock()
        query, remote = Query(clock), Remote()
        parent = Mock()
        parent.alive.return_value = False
        def create_engine(plan, policy, store, q, r, **kwargs):
            return Engine(plan, policy, store, q, r, clock=clock, sleep=clock.sleep, wall=clock, **kwargs)
        with patch('moyu_tools.runtime.sys.platform', 'win32'), \
             patch('moyu_tools.bridge.Bridge'), patch('moyu_tools.bridge.ParentHandle', return_value=parent), \
             patch('moyu_tools.native.Driver'), patch('moyu_tools.native.LocalQuery', return_value=query), \
             patch('moyu_tools.native.RemoteFiles', return_value=remote), \
             patch('moyu_tools.runtime.Engine', side_effect=create_engine):
            self.assertEqual(worker(self.path), 2)
        self.assertEqual(query.clicks, 0)
        self.assertFalse(remote.pastes)

    def test_old_or_contradictory_worker_receipt_never_counts_as_success(self):
        progress = Path(self.req['task'])/'progress.json'
        progress.write_text('{}', encoding='utf-8')
        result = {'request_id': 'old', 'request_sha256': digest(self.req), 'ok': True,
                  'status': 'completed', 'progress_sha256': hashlib.sha256(progress.read_bytes()).hexdigest()}
        atomic_json_write(Path(self.req['result']), result)
        with self.assertRaises(WDMSError):
            verify_child(0, self.req)
        result['request_id'] = self.req['request_id']
        atomic_json_write(Path(self.req['result']), result)
        with self.assertRaises(WDMSError):
            verify_child(2, self.req)

    def test_progress_edit_after_receipt_is_rejected(self):
        progress = Path(self.req['task'])/'progress.json'
        progress.write_text('{}', encoding='utf-8')
        atomic_json_write(Path(self.req['result']), {'request_id': self.req['request_id'], 'request_sha256': digest(self.req),
                          'ok': True, 'status': 'completed', 'progress_sha256': '0'*64})
        with self.assertRaises(WDMSError):
            verify_child(0, self.req)

    def test_exit_zero_and_complete_label_cannot_hide_pending_segments(self):
        from moyu_tools.settings import profile_digest
        store = Store(self.req['task'])
        store.load_or_create(Plan.parse(self.req['plan']), self.output, profile_digest(profile()), profile()['wdms']['conditions_hash'])
        store.value['status'] = 'completed'
        store.commit()
        atomic_json_write(Path(self.req['result']), {'request_id':self.req['request_id'], 'request_sha256':digest(self.req),
                          'ok':True, 'status':'completed', 'progress_sha256':hashlib.sha256(store.path.read_bytes()).hexdigest()})
        with self.assertRaises(WDMSError) as caught:
            verify_child(0, self.req)
        self.assertEqual(caught.exception.code, 'MOYU_WORKER_UNCONFIRMED')

    def test_binding_window_separation_and_bounds_are_validated_without_asserts(self):
        valid = profile()
        self.assertIs(validate_profile(valid), valid)
        for edit in [lambda p: p['remote'].update(root=p['wdms']['root']),
                     lambda p: p['remote'].update(count=[-100, 0, 10, 20]),
                     lambda p: p['wdms']['root'].update(dpi=0),
                     lambda p: p['wdms']['count'].update(point=[800,550])]:
            bad = profile()
            edit(bad)
            with self.assertRaises(WDMSError):
                validate_profile(bad)

    def test_human_confirmed_rearm_archives_partial_copy_without_discarding_it(self):
        from moyu_tools.settings import profile_digest
        store = Store(self.req['task'])
        store.load_or_create(Plan.parse(self.req['plan']), self.output, profile_digest(profile()), profile()['wdms']['conditions_hash'])
        store.update(0, 'copying', count=1, number='001234', selected={'name':'987654.zip', 'size':5, 'sequence':1})
        stage = store.stage_folder(0)
        stage.mkdir(parents=True)
        (stage/'987654.zip').write_bytes(b'part')
        self.config['task'] = self.req['task']
        rearm(self.config, profile())
        value = load_json(store.path)
        backup = Path(value['segments'][0]['recovery_backups'][-1])
        self.assertEqual((backup/'987654.zip').read_bytes(), b'part')
        self.assertFalse(stage.exists())
        self.assertEqual(value['segments'][0]['phase'], 'queried')
        self.assertFalse(value['segments'][0]['rearm_pending'])

    def test_failed_archive_keeps_original_and_blocks_automatic_continuation(self):
        from moyu_tools.settings import profile_digest
        store = Store(self.req['task'])
        store.load_or_create(Plan.parse(self.req['plan']), self.output, profile_digest(profile()), profile()['wdms']['conditions_hash'])
        store.update(0, 'copying', count=1, number='001234', selected={'name':'987654.zip','size':5,'sequence':1})
        stage=store.stage_folder(0)
        stage.mkdir(parents=True)
        (stage/'987654.zip').write_bytes(b'part')
        self.config['task']=self.req['task']
        with patch.object(Path,'rename',side_effect=PermissionError('synthetic active copy')), self.assertRaises(PermissionError):
            rearm(self.config,profile())
        self.assertTrue(load_json(store.path)['segments'][0]['rearm_pending'])
        self.assertEqual((stage/'987654.zip').read_bytes(),b'part')

    def test_rebinding_reopened_windows_preserves_done_and_invalidates_trial_gate(self):
        from moyu_tools.files import inspect_zip
        from moyu_tools.settings import profile_digest
        old=profile()
        plan=Plan(date(2025,1,1),date(2025,1,14))
        store=Store(self.req['task'])
        store.load_or_create(plan,self.output,profile_digest(old),old['wdms']['conditions_hash'])
        destination=self.output/'987654.zip'
        data=zip_bytes()
        destination.write_bytes(data)
        old_binding=profile_digest(old)
        store.update(0,'done',count=1,number='001234',file=inspect_zip(destination),query_binding=old_binding,source_binding=old_binding)
        store.update(1,'querying',busy_seen=True,submitted_wall=0)
        store.value['trial_passed']=True
        store.commit()
        new=profile()
        new['wdms']['root']['hwnd']+=1000
        new['wdms']['root']['pid']+=1
        for key in ('start','end','query','count_label','count','header','cell'):
            new['wdms'][key]['hwnd']+=1000
            new['wdms'][key]['pid']+=1
        self.config.update(plan=plan.document(),task=self.req['task'])
        adopt_binding(self.config,new)
        value=Store(self.req['task']).load_or_create(plan,self.output,profile_digest(new),new['wdms']['conditions_hash'])
        self.assertEqual(value['segments'][0]['phase'],'done')
        self.assertEqual(value['segments'][1]['phase'],'pending')
        self.assertFalse(value['trial_passed'])
        self.assertEqual(destination.read_bytes(),data)

    def test_new_binding_with_changed_business_conditions_cannot_adopt_progress(self):
        from moyu_tools.settings import profile_digest
        store=Store(self.req['task'])
        store.load_or_create(Plan.parse(self.req['plan']),self.output,profile_digest(profile()),profile()['wdms']['conditions_hash'])
        before=store.path.read_bytes()
        new=profile()
        new['wdms']['conditions_hash']='c'*64
        self.config['task']=self.req['task']
        with self.assertRaises(WDMSError):
            adopt_binding(self.config,new)
        self.assertEqual(store.path.read_bytes(),before)

    def test_bootstrap_and_helpers_cannot_call_network_or_old_workflow(self):
        base = Path(__file__).resolve().parents[1]/'moyu_tools'
        for filename in ('windows_native.cs', 'windows_bridge.ps1', 'launch.ps1'):
            source = (base/filename).read_text(encoding='utf-8-sig')
            self.assertNotIn('Invoke-WebRequest', source)
            self.assertNotIn('HttpClient', source)
            self.assertNotIn('Invoke-Expression', source)
        source = (base/'runtime.py').read_text(encoding='utf-8')
        self.assertNotIn('desktop_demo.login', source)
        self.assertNotIn('--native-workflow', source)

    def test_corrupt_progress_cannot_forge_passed_trial(self):
        from moyu_tools.settings import profile_digest
        store = Store(self.req['task'])
        plan = Plan.parse(self.req['plan'])
        store.load_or_create(plan, self.output, profile_digest(profile()))
        value = json.loads(store.path.read_text(encoding='utf-8'))
        value['trial_passed'] = True
        atomic_json_write(store.path, value)
        with self.assertRaises(WDMSError):
            Store(self.req['task']).load_or_create(plan, self.output, profile_digest(profile()))
