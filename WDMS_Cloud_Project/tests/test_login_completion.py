"""Read-only completion waiting for one submitted login; all data is synthetic."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_demo import login
from errors import WDMSError


class LoginCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.log=self.folder/'login.jsonl'
        self.clock=[0.0]
        self.closed=60.0
        self.home_at=60.0
        self.enabled_at=60.0
        self.markers_at=60.0
        self.root={'hwnd':10,'pid':7}
        self.main={'hwnd':20,'pid':7,'visible':True,'class_name':'WindowsForms10.Window.8.synthetic'}
        self.api=SimpleNamespace(user32=SimpleNamespace(IsWindow=lambda hwnd:hwnd==10,
            IsWindowVisible=lambda hwnd:self.clock[0]<self.closed,
            IsWindowEnabled=lambda hwnd:self.clock[0]>=self.enabled_at),
            probe_login_ready=mock.Mock(),metadata=lambda hwnd:dict(self.main),
            click_query_input=mock.Mock(),phase_context=None)
        self.scan_paths=[]
        for patch in (mock.patch.object(login.time,'monotonic',side_effect=lambda:self.clock[0]),
                      mock.patch.object(login.time,'sleep',side_effect=lambda seconds:self.clock.__setitem__(0,self.clock[0]+seconds))):
            patch.start();self.addCleanup(patch.stop)

    def find(self,api,stage,log):
        if self.clock[0]<self.home_at:
            raise WDMSError('STARTUP_WINDOW_NOT_UNIQUE','当前找到 0 个。')
        return dict(self.main)

    def scan(self,api,main,log,**kwargs):
        self.scan_paths.append(log)
        login.probe.record(log,'native_scan_complete')
        events=[]
        if self.clock[0]>=self.markers_at:
            events=[{'event':'native_text','text':'在库管理','visible':True}]
        for event in events:
            login.probe.record(log,event['event'],**{key:value for key,value in event.items() if key!='event'})
        return events

    def wait(self,**kwargs):
        with mock.patch.object(login,'find_stage_window',side_effect=self.find), \
             mock.patch.object(login,'pid_matches',return_value=True), \
             mock.patch.object(login.probe,'check_native_controls',side_effect=self.scan):
            return login.wait_login_completion(self.api,self.root,'Synthetic.exe',self.log,**kwargs)

    def test_login_and_loading_stay_visible_for_60_seconds_then_succeed(self):
        main,markers=self.wait()
        self.assertEqual((main['pid'],markers),(7,['在库管理']))
        self.assertGreaterEqual(self.clock[0],60)
        self.assertLess(self.clock[0],61)
        self.api.click_query_input.assert_not_called()
        self.assertTrue((self.folder/'login_home.jsonl').exists())

    def test_fast_login_returns_without_waiting_two_minutes(self):
        self.closed=self.home_at=self.enabled_at=self.markers_at=0
        self.wait()
        self.assertEqual(self.clock[0],0)

    def test_home_can_appear_later_after_login_window_disappears(self):
        self.closed=55;self.home_at=self.enabled_at=self.markers_at=70
        self.wait()
        self.assertGreaterEqual(self.clock[0],70)
        self.assertLess(self.clock[0],71)

    def test_home_must_be_enabled_and_have_fresh_markers(self):
        self.closed=self.home_at=50;self.enabled_at=55;self.markers_at=60
        self.wait()
        self.assertGreaterEqual(self.clock[0],60)
        self.assertGreater(len(self.scan_paths),1)
        self.assertEqual(len(self.scan_paths),len(set(self.scan_paths)))

    def test_visible_login_at_deadline_stops_without_retry(self):
        self.closed=200
        with self.assertRaises(WDMSError) as caught:self.wait()
        self.assertEqual(caught.exception.code,'LOGIN_NOT_COMPLETED')
        self.assertAlmostEqual(self.clock[0],120)
        self.assertEqual(self.scan_paths,[])
        self.api.click_query_input.assert_not_called()

    def test_home_delay_does_not_receive_a_new_120_second_budget(self):
        self.closed=110;self.home_at=130
        with self.assertRaises(WDMSError) as caught:self.wait()
        self.assertEqual(caught.exception.code,'LOGIN_HOME_WINDOW_TIMEOUT')
        self.assertAlmostEqual(self.clock[0],120)

    def test_old_home_evidence_cannot_make_a_new_scan_pass(self):
        self.closed=self.home_at=self.enabled_at=0;self.markers_at=200
        login.probe.record(self.folder/'login_home.jsonl','native_text',text='在库管理')
        with self.assertRaises(WDMSError) as caught:self.wait()
        self.assertEqual(caught.exception.code,'LOGIN_HOME_MARKER_UNCONFIRMED')
        self.assertAlmostEqual(self.clock[0],120)

    def test_changed_process_stops_immediately(self):
        self.closed=self.home_at=0;self.main['pid']=8
        with self.assertRaises(WDMSError) as caught:self.wait()
        self.assertEqual(caught.exception.code,'LOGIN_MAIN_WINDOW_MISMATCH')
        self.assertEqual(self.clock[0],0)

    def test_multiple_home_candidates_are_not_retried_as_network_delay(self):
        self.closed=0
        with mock.patch.object(self,'find',side_effect=WDMSError('STARTUP_WINDOW_NOT_UNIQUE','当前找到 2 个。')):
            with self.assertRaises(WDMSError) as caught:self.wait()
        self.assertEqual(caught.exception.code,'STARTUP_WINDOW_NOT_UNIQUE')
        self.assertEqual(self.clock[0],0)

    def test_access_denied_is_not_retried(self):
        self.closed=self.home_at=self.enabled_at=0
        self.api.probe_login_ready.side_effect=WDMSError('WDMS_ACCESS_DENIED','synthetic denied')
        with self.assertRaises(WDMSError) as caught:self.wait()
        self.assertEqual(caught.exception.code,'WDMS_ACCESS_DENIED')
        self.api.probe_login_ready.assert_called_once()

    def test_transient_home_probe_waits_without_resubmitting_login(self):
        self.closed=self.home_at=self.enabled_at=self.markers_at=0
        self.api.probe_login_ready.side_effect=[WDMSError('WDMS_UNRESPONSIVE','synthetic loading'),None]
        self.wait()
        self.assertGreaterEqual(self.clock[0],2)
        self.api.click_query_input.assert_not_called()

    def test_outer_deadline_is_honoured_before_two_minutes(self):
        self.closed=200
        with self.assertRaises(WDMSError) as caught:self.wait(execution_deadline=10)
        self.assertEqual(caught.exception.code,'STEP_TIMEOUT')
        self.assertAlmostEqual(self.clock[0],10)

    def test_cancel_request_stops_readonly_wait(self):
        cancel=self.folder/'cancel.request';cancel.touch()
        with self.assertRaises(WDMSError) as caught:self.wait(cancel_file=cancel)
        self.assertEqual(caught.exception.code,'TASK_CANCELLED')
        self.assertEqual(self.clock[0],0)

    def test_late_scan_cannot_confirm_success_past_deadline_and_restores_context(self):
        self.closed=self.home_at=self.enabled_at=self.markers_at=0
        original=self.scan
        def slow(*args,**kwargs):
            events=original(*args,**kwargs);self.clock[0]=120;return events
        self.scan=slow
        with self.assertRaises(WDMSError):self.wait()
        self.assertIsNone(self.api.phase_context)

    def test_parent_supervisor_budget_and_worker_arguments(self):
        cancel=self.folder/'cancel.request'
        with mock.patch.object(login.probe,'supervise',return_value=0) as supervisor, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(login.main(['--out',str(self.folder/'parent')]),0)
            self.assertEqual(supervisor.call_args.args[2],420)
            self.assertEqual(login.main(['--out',str(self.folder/'limited'),'--execution-deadline','50',
                                         '--cancel-file',str(cancel)]),0)
            self.assertEqual(supervisor.call_args.args[2],50)
            command=supervisor.call_args.args[0]
            self.assertEqual(command[command.index('--execution-deadline')+1],'50.0')
            self.assertEqual(command[command.index('--cancel-file')+1],str(cancel.resolve()))
