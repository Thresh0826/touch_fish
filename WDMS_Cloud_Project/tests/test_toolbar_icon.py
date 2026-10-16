import copy
import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_demo import toolbar_icon as icon
from errors import WDMSError


class ToolbarIconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name)/'log.jsonl'
        self.root = {'hwnd': 100, 'pid': 7, 'rect': [0, 0, 1200, 800]}
        self.toolbar = {'hwnd': 200, 'pid': 7, 'class_name': 'WindowsForms10.Window.8.app',
                        'rect': [0, 24, 1200, 84], 'visible': True, 'depth': 1}
        self.match = {'revision': 'toolbar-icon-v2', 'template_id': 18,
                      'rect': [78, 6, 108, 36], 'score': .91, 'runner_up': .72,
                      'scale_height': 38, 'elapsed_ms': 240, 'button_rect': [70, 24, 124, 80],
                      'button_state': 0, 'button_role': 43}
        self.target = icon.target_from_match(self.match, self.toolbar)

    def request(self):
        return {'root_hwnd': 100, 'pid': 7, 'containers': [self.toolbar]}

    def events(self):
        return [{'event': 'menu_msaa_container_complete', 'container_hwnd': 200,
                 'root_role': 22, 'truncated': False}]

    def test_selects_complete_toolbar_even_when_other_containers_are_partial(self):
        req = self.request()
        req['containers'].append({**self.toolbar, 'hwnd': 201, 'rect': [0, 84, 200, 800]})
        es = self.events()+[{'event': 'menu_msaa_container_pruned', 'container_hwnd': 201}]
        self.assertEqual(icon.choose_toolbar(req, es, self.root)['hwnd'], 200)

    def test_rejects_changed_root_truncated_toolbar_and_ambiguous_candidates(self):
        req = self.request()
        req['pid'] = 9
        with self.assertRaises(WDMSError):icon.choose_toolbar(req, self.events(), self.root)
        es = self.events();es[0]['truncated'] = True
        with self.assertRaises(WDMSError):icon.choose_toolbar(self.request(), es, self.root)
        req = self.request();req['containers'].append({**self.toolbar, 'hwnd': 201})
        with self.assertRaises(WDMSError):
            icon.choose_toolbar(req, self.events()+[{**self.events()[0], 'container_hwnd': 201}], self.root)

    def test_account_dependent_horizontal_position_is_taken_from_match(self):
        self.assertEqual(self.target['icon_rect'], [78, 30, 108, 60])
        data = copy.deepcopy(self.match)
        for key in ('rect', 'button_rect'):
            data[key][0] += 300; data[key][2] += 300
        self.assertEqual(icon.target_from_match(data, self.toolbar)['icon_rect'], [378, 30, 408, 60])

    def test_low_score_and_duplicate_physical_targets_are_rejected(self):
        for key,value,code in (('score', .85, 'ICON_LOW_CONFIDENCE'),
                               ('runner_up', .89, 'ICON_TARGET_NOT_UNIQUE')):
            data = copy.deepcopy(self.match);data[key] = value
            with self.subTest(key=key), self.assertRaises(WDMSError) as error:
                icon.target_from_match(data, self.toolbar)
            self.assertEqual(error.exception.code, code)

    def test_disabled_invisible_offscreen_or_non_button_target_is_rejected(self):
        for key,value in (('button_state', 1), ('button_state', 32768), ('button_state', 65536),
                          ('button_state', -1), ('button_state', True), ('button_role', 22),
                          ('button_rect', [0,24,50,80])):
            data = copy.deepcopy(self.match);data[key] = value
            with self.subTest(value=value), self.assertRaises(WDMSError):
                icon.target_from_match(data, self.toolbar)

    def test_result_protocol_and_non_finite_scores_are_rejected(self):
        for key,value in (('revision', 'wrong'), ('revision', 'toolbar-icon-v1'),
                          ('template_id', 19), ('score', float('nan')),
                          ('score', True), ('runner_up', float('inf')), ('scale_height', 113),
                          ('elapsed_ms', 5001)):
            data = copy.deepcopy(self.match);data[key] = value
            with self.subTest(key=key), self.assertRaises(WDMSError):
                icon.target_from_match(data, self.toolbar)
        data = copy.deepcopy(self.match);data['raw_text'] = 'private-test-label'
        with self.assertRaises(WDMSError):icon.target_from_match(data, self.toolbar)

    def test_invalid_geometry_is_rejected(self):
        for rect in ([float('nan'),6,108,36], [True,6,108,36], [-1,6,29,36],
                     [1190,6,1220,36], [78,6,108,90], [78,6,78,36], [78,6,109,36]):
            data = copy.deepcopy(self.match);data['rect'] = rect
            with self.subTest(rect=rect), self.assertRaises(WDMSError):
                icon.target_from_match(data, self.toolbar)

    def api(self, sent=2):
        cursor = [0, 0]
        def move(x, y):cursor[:] = [x, y];return True
        def position(ptr):
            point = ctypes.cast(ptr, ctypes.POINTER(icon.probe.wintypes.POINT)).contents
            point.x, point.y = cursor
            return True
        u = SimpleNamespace(IsWindowEnabled=lambda hwnd: True, GetForegroundWindow=lambda: 100,
                            WindowFromPoint=lambda pt: 200, GetAsyncKeyState=lambda key: 0,
                            SetCursorPos=move, GetCursorPos=position, SendInput=mock.Mock(return_value=sent))
        return SimpleNamespace(metadata=lambda hwnd: self.toolbar, user32=u, health=mock.Mock())

    def test_verified_target_sends_one_down_up_pair(self):
        api = self.api()
        with mock.patch.object(icon, 'capture_target', return_value=self.target), mock.patch.object(icon.time, 'sleep'):
            icon.click_target(api, self.root, self.target, self.log)
        api.user32.SendInput.assert_called_once()
        args = api.user32.SendInput.call_args.args
        self.assertEqual(args[0], 2)
        self.assertEqual([args[1][i].mi.dwFlags for i in range(2)], [2, 4])

    def test_second_recognition_changed_or_obscured_target_never_clicks(self):
        for changed in (True, False):
            api = self.api()
            target = {**self.target, 'icon_rect': [71, 59, 121, 71]} if changed else self.target
            if not changed:api.user32.WindowFromPoint = lambda pt: 999
            with mock.patch.object(icon, 'capture_target', return_value=target), self.assertRaises(WDMSError):
                icon.click_target(api, self.root, self.target, self.log)
            api.user32.SendInput.assert_not_called()

    def test_cancelled_or_busy_input_never_clicks(self):
        api = self.api();cancel = Path(self.temp.name)/'cancel';cancel.touch()
        with self.assertRaises(WDMSError):icon.click_target(api, self.root, self.target, self.log, cancel)
        api.user32.SendInput.assert_not_called()
        api.user32.GetAsyncKeyState = lambda key: 0x8000
        with mock.patch.object(icon, 'capture_target', return_value=self.target), self.assertRaises(WDMSError):
            icon.click_target(api, self.root, self.target, self.log)
        api.user32.SendInput.assert_not_called()

    def test_partial_input_only_releases_mouse_without_another_down(self):
        api = self.api(sent=1)
        with mock.patch.object(icon, 'capture_target', return_value=self.target), \
             mock.patch.object(icon.time, 'sleep'), self.assertRaises(WDMSError):
            icon.click_target(api, self.root, self.target, self.log)
        self.assertEqual(api.user32.SendInput.call_count, 2)
        release = api.user32.SendInput.call_args.args[1]
        self.assertEqual(ctypes.cast(release, ctypes.POINTER(icon.probe.NativeInput)).contents.mi.dwFlags, 4)

    def test_capture_passes_pinned_template_without_persisting_screen_and_cleans_temp(self):
        paths = []
        def supervise(command, log, timeout):
            self.assertEqual(timeout, 20)
            self.assertNotIn('-ImageFile', command)
            self.assertEqual(Path(command[command.index('-TemplateFile')+1]).name, 'toolbar_inventory_18.png')
            output = Path(command[command.index('-OutputFile')+1]);paths.append(output.parent)
            output.write_text(json.dumps(self.match), encoding='utf-8-sig')
            return 0
        with mock.patch.object(icon, '_powershell_executable', return_value='powershell.exe'), \
             mock.patch.object(icon.probe, 'supervise', side_effect=supervise):
            icon.capture_target(self.root, self.toolbar, self.log)
        self.assertFalse(paths[0].exists())
        data = icon.probe.read_events(self.log)[-1]
        self.assertEqual(data['template_id'], 18)
        self.assertFalse(data['image_saved']);self.assertFalse(data['network_used'])

    def test_helper_failure_is_preserved_and_temp_is_removed(self):
        paths = []
        def supervise(command, log, timeout):
            output = Path(command[command.index('-OutputFile')+1]);paths.append(output.parent)
            output.write_text('{"error_code":"ICON_MATCH_TIMEOUT"}', encoding='utf-8')
            return 0
        with mock.patch.object(icon, '_powershell_executable', return_value='powershell.exe'), \
             mock.patch.object(icon.probe, 'supervise', side_effect=supervise), self.assertRaises(WDMSError) as error:
            icon.capture_target(self.root, self.toolbar, self.log)
        self.assertEqual(error.exception.code, 'ICON_MATCH_TIMEOUT')
        self.assertFalse(paths[0].exists())

    def test_cancellation_before_capture_never_starts_helper(self):
        cancel = Path(self.temp.name)/'cancel';cancel.touch()
        with mock.patch.object(icon.probe, 'supervise') as helper, self.assertRaises(WDMSError):
            icon.capture_target(self.root, self.toolbar, self.log, cancel)
        helper.assert_not_called()

    def test_corrupted_template_is_rejected_before_helper(self):
        with mock.patch.object(icon, 'TEMPLATE_SHA256', '0'*64), \
             mock.patch.object(icon.probe, 'supervise') as helper, self.assertRaises(WDMSError) as error:
            icon.capture_target(self.root, self.toolbar, self.log)
        helper.assert_not_called();self.assertEqual(error.exception.code, 'ICON_TEMPLATE_INVALID')

    def test_low_match_reports_scores_and_cleans_temp_without_click(self):
        paths=[]
        def supervise(command, log, timeout):
            output=Path(command[command.index('-OutputFile')+1]);paths.append(output.parent)
            data=copy.deepcopy(self.match);data['score']=.7
            output.write_text(json.dumps(data), encoding='utf-8');return 0
        with mock.patch.object(icon, '_powershell_executable', return_value='powershell.exe'), \
             mock.patch.object(icon.probe, 'supervise', side_effect=supervise), self.assertRaises(WDMSError):
            icon.capture_target(self.root,self.toolbar,self.log)
        self.assertFalse(paths[0].exists())
        event=icon.probe.read_events(self.log)[-1]
        self.assertEqual(event['event'],'toolbar_icon_rejected');self.assertEqual(event['score'],.7)
        self.assertEqual(event['scale_height'],self.match['scale_height'])
        self.assertEqual(event['match_rect'],self.match['rect'])
        self.assertEqual(event['toolbar_rect'],self.toolbar['rect'])
        self.assertEqual(event['matcher_revision'],'toolbar-icon-v2')
        self.assertEqual(event['min_score'],.86)
        self.assertEqual(event['min_margin'],.06)

    def test_onsite_below_threshold_score_still_cannot_be_accepted(self):
        data=copy.deepcopy(self.match);data['score']=0.8534236305335552
        with self.assertRaises(WDMSError) as error:
            icon.target_from_match(data,self.toolbar)
        self.assertEqual(error.exception.code,'ICON_LOW_CONFIDENCE')


if __name__ == '__main__':unittest.main()
