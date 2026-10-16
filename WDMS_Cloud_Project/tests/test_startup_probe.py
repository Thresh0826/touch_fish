import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from scripts import check_wdms_startup as startup
from errors import WDMSError

class StartupProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/'startup.jsonl'

    def api(self,windows):
        def enumerate_windows(callback,unused):
            for hwnd in windows:
                if not callback(hwnd,0):return False
            return True
        return SimpleNamespace(callback_type=lambda callback:callback,
            title=lambda hwnd:windows[hwnd]['title'],metadata=lambda hwnd,include_title=False:windows[hwnd],
            user32=SimpleNamespace(EnumWindows=enumerate_windows,IsWindowVisible=lambda hwnd:windows[hwnd].get('visible',True)))

    def test_owned_login_window_is_found(self):
        windows={2:{'hwnd':2,'title':'用户登录','owner_hwnd':1,'pid':10,'minimized':False}}
        result=startup.find_stage_window(self.api(windows),'login',self.log)
        self.assertEqual(result['hwnd'],2)

    def test_hidden_login_is_not_treated_as_ready(self):
        windows={2:{'hwnd':2,'title':'用户登录','visible':False}}
        with self.assertRaises(WDMSError):
            startup.find_stage_window(self.api(windows),'login',self.log)

    def test_duplicate_login_windows_stop_inspection(self):
        windows={i:{'hwnd':i,'title':'用户登录'} for i in (1,2)}
        with self.assertRaises(WDMSError) as caught:
            startup.find_stage_window(self.api(windows),'login',self.log)
        self.assertEqual(caught.exception.code,'STARTUP_WINDOW_NOT_UNIQUE')

    def test_inspection_uses_read_only_native_scan(self):
        api=SimpleNamespace(health=mock.Mock())
        args=SimpleNamespace(log=str(self.log),stage='login')
        with mock.patch.object(startup.probe,'inspect_environment',return_value={}),mock.patch.object(startup.probe,'require_usable_environment'),mock.patch.object(startup,'TrialNativeWindows',return_value=api),mock.patch.object(startup,'find_stage_window',return_value={'hwnd':2}),mock.patch.object(startup.probe,'check_native_controls') as scan:
            code=startup.child(args)
        self.assertEqual(code,0)
        scan.assert_called_once_with(api,{'hwnd':2},self.log,max_depth=8,budget=25,read_labels=True)
        complete=startup.probe.read_events(self.log)[-1]
        self.assertFalse(complete['credential_fields_read'])
        self.assertFalse(complete['actions_performed'])

if __name__=='__main__':unittest.main()
