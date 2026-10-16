import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_demo import owner_discovery as discovery
from errors import NativeTextReadError
from services import owner_catalog


class OwnerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.log=self.root/'capture.jsonl'
        self.snapshot=self.root/'owners_snapshot.json'
        self.config=SimpleNamespace(wdms_exe=str(self.root/'Synthetic.exe'),username='synthetic-user',credential_target='synthetic-target')
        self.window={'hwnd':100,'pid':7,'class_name':'WindowsForms10.Window.8.root','visible':True,'minimized':False,'rect':[0,0,1000,800]}
        self.control={'hwnd':10,'pid':7,'class_name':'WindowsForms10.COMBOBOX.owner','visible':True,'rect':[300,100,500,125]}
        self.names=[f'合成发现货主{i:02d}' for i in range(24)]
        self.api=mock.Mock();self.api.user32.IsWindowEnabled.return_value=True
        self.api.metadata.side_effect=lambda hwnd:dict(self.window if hwnd==100 else self.control)
        self.api.read_owner_list.return_value=self.names

    def child(self,page=True,pid=True,visible=True,scan_error=None,cancel=None):
        self.scans=[]
        def scan(api,root,log,**kwargs):
            self.scans.append(log)
            if scan_error is not None and len(self.scans)==1:
                discovery.probe.record(log,'before_native_text',hwnd=90,class_name='WindowsForms10.STATIC.synthetic')
                raise scan_error
            discovery.probe.record(log,'native_scan_complete')
        with mock.patch.object(discovery,'load_settings',return_value=self.config), \
             mock.patch.object(discovery,'validate_executable',return_value=self.config.wdms_exe), \
             mock.patch.object(discovery.probe,'inspect_environment'), \
             mock.patch.object(discovery.probe,'require_usable_environment'), \
             mock.patch.object(discovery.navigation,'NavigationNativeWindows',return_value=self.api), \
             mock.patch.object(discovery,'find_stage_window',return_value=self.window), \
             mock.patch.object(discovery.login,'pid_matches',return_value=pid), \
             mock.patch.object(discovery.probe,'check_native_controls',side_effect=scan), \
             mock.patch.object(discovery.navigation,'supplement_navigation_captions',return_value=[]), \
             mock.patch.object(discovery.navigation,'confirm_inventory_page',return_value=page), \
             mock.patch.object(discovery.navigation,'inventory_page_controls',return_value={'owner':self.control}), \
             mock.patch.object(discovery.navigation,'inspect_geometry',return_value={'visible_rect':self.control['rect'] if visible else None}), \
             mock.patch.object(discovery.time,'sleep'):
            return discovery.child(SimpleNamespace(log=str(self.log),snapshot=self.snapshot,cancel_file=cancel))

    def assert_no_input(self):
        for name in ('SendInput','SetForegroundWindow','SetCursorPos'):
            getattr(self.api.user32,name).assert_not_called()
        self.api.click_query_input.assert_not_called()

    def test_stable_list_read_twice_and_snapshot_is_bound_to_current_session(self):
        self.assertEqual(self.child(),0)
        document,names,digest=owner_catalog.read_plan(self.snapshot)
        self.assertEqual(names,tuple(self.names));self.assertEqual(document['identity'],{'pid':7,'main_hwnd':100})
        self.assertEqual(self.api.read_owner_list.call_count,2);self.assert_no_input()
        event=discovery.probe.read_events(self.log)[-1]
        self.assertEqual(event['event'],'owner_list_capture_complete');self.assertFalse(event['query_performed'])

    def test_transient_page_read_timeout_recovers_before_reading_names(self):
        error=NativeTextReadError('NATIVE_TEXT_READ_FAILED','合成读取超时',winerror=1460,
                                  hwnd=90,class_name='WindowsForms10.STATIC.synthetic')
        self.assertEqual(self.child(scan_error=error),0)
        self.assertEqual(len(self.scans),2);self.assertNotEqual(*self.scans)
        failed=discovery.probe.read_events(self.scans[0])[-1]
        self.assertEqual((failed['event'],failed['winerror']),('native_scan_failed',1460))
        self.assertEqual(self.api.read_owner_list.call_count,2);self.assert_no_input()
        self.assertTrue(any(e['event']=='page_scan_recovered' for e in discovery.probe.read_events(self.log)))

    def test_page_access_denied_and_cancel_prevent_any_name_read(self):
        error=NativeTextReadError('NATIVE_TEXT_READ_FAILED','合成拒绝访问',winerror=5,
                                  hwnd=90,class_name='WindowsForms10.STATIC.synthetic')
        self.assertEqual(self.child(scan_error=error),2)
        self.api.read_owner_list.assert_not_called();self.assertFalse(self.snapshot.exists())
        self.assertEqual(discovery.probe.read_events(self.log)[-1]['winerror'],5)
        cancel=self.root/'cancel.request';cancel.touch()
        self.assertEqual(self.child(cancel=cancel),2)
        self.assertEqual(self.scans,[]);self.assert_no_input()

    def test_unconfirmed_page_foreign_process_and_clipped_combo_never_save(self):
        for options in ({'page':False},{'pid':False},{'visible':False}):
            with self.subTest(options=options):
                self.assertEqual(self.child(**options),2)
                self.assertFalse(self.snapshot.exists());self.assert_no_input()

    def test_changed_order_between_reads_stops_without_snapshot_or_query(self):
        self.api.read_owner_list.side_effect=[self.names,list(reversed(self.names))]
        self.assertEqual(self.child(),2);self.assertFalse(self.snapshot.exists());self.assert_no_input()
        self.assertEqual(discovery.probe.read_events(self.log)[-1]['error_code'],'OWNER_LIST_UNSTABLE')

    def test_disabled_combo_stops_before_reading_names(self):
        self.api.user32.IsWindowEnabled.return_value=False
        self.assertEqual(self.child(),2);self.api.read_owner_list.assert_not_called();self.assert_no_input()

    def test_changed_root_after_reads_prevents_snapshot(self):
        self.api.metadata.side_effect=lambda hwnd:{**self.window,'pid':99} if hwnd==100 else dict(self.control)
        self.assertEqual(self.child(),2);self.assertFalse(self.snapshot.exists());self.assert_no_input()

    def test_capture_parent_requires_both_marker_and_matching_snapshot(self):
        with mock.patch.object(discovery.probe,'supervise',return_value=0):
            code=discovery.main(['--out',str(self.root),'--snapshot',str(self.snapshot)])
        self.assertEqual(code,2)

    def test_capture_parent_supervises_new_worker_route_and_passes_cancel(self):
        cancel=self.root/'cancel.request'
        def supervisor(command,log,timeout,cancelled=None):
            self.assertIn('--native-owners',command)
            self.assertEqual(timeout,75)
            self.assertFalse(cancelled())
            self.assertEqual(command[command.index('--cancel-file')+1],str(cancel.resolve()))
            owner_catalog.write_snapshot(self.snapshot,self.names,self.config,self.window)
            discovery.probe.record(log,'owner_list_capture_complete',count=24,
                fingerprint=owner_catalog.fingerprint(self.names),pid=7,main_hwnd=100)
            return 0
        with mock.patch.object(discovery.probe,'supervise',side_effect=supervisor):
            self.assertEqual(discovery.main(['--out',str(self.root),'--snapshot',str(self.snapshot),'--cancel-file',str(cancel)]),0)

    def test_cancel_after_child_return_prevents_capture_success(self):
        cancel=self.root/'cancel.request'
        def supervisor(command,log,timeout,cancelled=None):
            cancel.touch()
            return 0
        with mock.patch.object(discovery.probe,'supervise',side_effect=supervisor):
            self.assertEqual(discovery.main(['--out',str(self.root),'--snapshot',str(self.snapshot),'--cancel-file',str(cancel)]),2)


if __name__=='__main__':unittest.main()
