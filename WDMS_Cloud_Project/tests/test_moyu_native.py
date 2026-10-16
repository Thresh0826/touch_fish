from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from core.step_executor import StepContext
from errors import WDMSError
from moyu_tools.native import Driver, LocalQuery, RemoteFiles, confirm_column, remote_result, row_count
from tests.moyu_fixtures import Clock, profile


def regions(count, name='987654.zip'):
    return [{'text': name if count else '', 'lines': [{'text': name, 'rect': [0, 0, 90, 18]}] if count else []},
            {'text': str(count), 'lines': []}]


class VirtualRemote:
    """Actual remote adapter protocol exercised against a synthetic remote desktop."""
    def __init__(self, clock):
        self.clock, self.bridge = clock, self
        self.keyword, self.selection = '', 'search'
        self.sequence, self.clip, self.inputs = 1, None, []
        self.filename, self.stale, self.zero_seen = '987654.zip', False, False
        self.layout = 'a'*64
        self.actions = []

    def focus(self, root, context):
        context.check()

    def guard(self, root, context):
        context.check()

    def click(self, root, point, context):
        self.selection = 'search' if point[1] < 90 else 'file'

    def type_text(self, root, text, context):
        self.keyword = text
        self.inputs.append(text)

    def chord(self, root, key, context, modifier=0x11):
        if key == 0x43:
            if not self.stale:
                self.sequence += 1
            self.clip = self.keyword if self.selection == 'search' else 'file'
        self.inputs.append(key)

    def call(self, action, payload=None, context=None):
        self.actions.append(action)
        self.clock.sleep(.05)
        if context:
            context.check()
        if action == 'hash':
            return {'hash': self.layout}
        if action == 'clear':
            self.sequence += 1
            self.clip = None
            return {'sequence': self.sequence}
        if action == 'text':
            return {'sequence': self.sequence, 'text': self.clip if isinstance(self.clip, str) else ''}
        if action == 'ocr':
            if self.keyword.startswith('__moyu_no_match_'):
                self.zero_seen = True
                return {'regions': regions(0)}
            return {'regions': regions(1)}
        if action == 'file':
            return {'sequence': self.sequence, 'name': self.filename, 'size': 123}
        if action == 'folder':
            return {'windows': [{**profile()['remote']['root'], 'class_name': 'CabinetWClass', 'hwnd': 300}]}
        if action in ('open_folder', 'navigate_folder'):
            return {}
        raise AssertionError(action)


class MoyuNativeTests(unittest.TestCase):
    def test_counts_require_complete_unambiguous_text(self):
        self.assertEqual(row_count('总行数： 1 行'), 1)
        self.assertEqual(row_count('0'), 0)
        for value in ('', '1/10', '合计1', '1 2', 'O'):
            with self.subTest(value=value), self.assertRaises(WDMSError):
                row_count(value)

    def test_remote_requires_one_zip_line_and_exact_numeric_count(self):
        self.assertEqual(remote_result(regions(1), 1)['name'], '987654.zip')
        self.assertEqual(remote_result(regions(0), 0), {})
        multiple = regions(1)
        multiple[0]['lines'] *= 2
        self.assertIsNone(remote_result(multiple, 1))
        unreadable = regions(1)
        unreadable[1]['text'] = '1 of 10'
        self.assertIsNone(remote_result(unreadable, 1))

    def test_stale_zip_cannot_be_accepted_as_zero_search_state(self):
        old = regions(1)
        old[1]['text'] = '0'
        self.assertIsNone(remote_result(old, 0))

    def test_remote_search_clears_old_results_verifies_keyword_and_actual_file(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        remote = RemoteFiles(driver, profile())
        with patch('moyu_tools.native.time.monotonic', clock), patch('moyu_tools.native.time.sleep', clock.sleep):
            selected = remote.search('001234', StepContext(30, lambda: False, clock))
        self.assertTrue(driver.zero_seen)
        self.assertEqual(selected['name'], '987654.zip')
        self.assertIn('001234', driver.inputs)
        self.assertNotEqual(selected['name'], '001234.zip')
        self.assertNotIn(0x0D, driver.inputs)

    def test_enter_to_submit_is_explicit_opt_in_not_file_open_default(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        p = profile()
        p['remote']['submit_enter'] = True
        remote = RemoteFiles(driver, p)
        with patch('moyu_tools.native.time.monotonic', clock), patch('moyu_tools.native.time.sleep', clock.sleep):
            remote.search('001234', StepContext(30, lambda: False, clock))
        self.assertEqual(driver.inputs.count(0x0D), 2)

    def test_stale_clipboard_sequence_never_accepted(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        driver.stale = True
        remote = RemoteFiles(driver, profile())
        with patch('moyu_tools.native.time.monotonic', clock), patch('moyu_tools.native.time.sleep', clock.sleep):
            with self.assertRaises(WDMSError) as caught:
                remote.search('001234', StepContext(3, lambda: False, clock))
        self.assertEqual(caught.exception.code, 'STEP_TIMEOUT')

    def test_clipboard_race_retries_only_the_read_not_typing_or_copy(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        remote = RemoteFiles(driver, profile())
        original = driver.call
        raced = [False]
        def call(action, payload=None, context=None):
            if action == 'text' and not raced[0]:
                raced[0] = True
                raise WDMSError('MOYU_NATIVE_UNAVAILABLE', 'synthetic clipboard synchronization')
            return original(action, payload, context)
        with patch.object(driver, 'call', side_effect=call), patch('moyu_tools.native.time.monotonic', clock), \
                patch('moyu_tools.native.time.sleep', clock.sleep):
            remote.search('001234', StepContext(30, lambda: False, clock))
        self.assertEqual(sum(isinstance(x, str) for x in driver.inputs), 2)
        self.assertEqual(driver.inputs.count(0x43), 3)  # sentinel text, real text, file

    def test_file_clipboard_name_mismatch_pauses_before_paste(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        driver.filename = '000.zip'
        remote = RemoteFiles(driver, profile())
        with patch('moyu_tools.native.time.monotonic', clock), patch('moyu_tools.native.time.sleep', clock.sleep):
            with self.assertRaises(WDMSError) as caught:
                remote.search('001234', StepContext(30, lambda: False, clock))
        self.assertEqual(caught.exception.code, 'MOYU_CLIPBOARD_MISMATCH')
        self.assertNotIn(0x56, driver.inputs)

    def test_remote_layout_change_stops_before_typing(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        driver.layout = 'b'*64
        remote = RemoteFiles(driver, profile())
        with self.assertRaises(WDMSError) as caught:
            remote.search('001234', StepContext(30, lambda: False, clock))
        self.assertEqual(caught.exception.code, 'MOYU_REMOTE_LAYOUT_CHANGED')
        self.assertFalse(driver.inputs)

    def test_changed_file_clipboard_after_switching_explorer_never_pastes(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        remote = RemoteFiles(driver, profile())
        selected = {'sequence': 999, 'name': '987654.zip', 'size': 123}
        with self.assertRaises(WDMSError) as caught:
            remote.paste(Path('synthetic-staging'), selected, StepContext(10, lambda: False, clock))
        self.assertEqual(caught.exception.code, 'MOYU_CLIPBOARD_CHANGED')
        self.assertNotIn(0x56, driver.inputs)
        self.assertEqual(driver.actions.count('open_folder'), 1)

    def test_normal_paste_is_sent_once_after_exact_folder_and_clipboard_check(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        remote = RemoteFiles(driver, profile())
        selected = {'sequence': 1, 'name': '987654.zip', 'size': 123}
        remote.paste(Path('synthetic-staging'), selected, StepContext(10, lambda: False, clock))
        self.assertEqual(driver.inputs.count(0x56), 1)

    def test_multiple_files_reuse_only_the_tools_own_staging_explorer(self):
        clock = Clock()
        driver = VirtualRemote(clock)
        remote = RemoteFiles(driver, profile())
        selected = {'sequence': 1, 'name': '987654.zip', 'size': 123}
        remote.paste(Path('stage-one'), selected, StepContext(10, lambda: False, clock))
        remote.paste(Path('stage-two'), selected, StepContext(10, lambda: False, clock))
        self.assertEqual(driver.actions.count('open_folder'), 1)
        self.assertEqual(driver.actions.count('navigate_folder'), 1)

    def test_hidden_column_restores_recorded_scroll_then_checks_header(self):
        driver = Mock()
        p = profile()
        p['wdms']['scroll'] = 123
        query = LocalQuery(driver, p)
        query.read = lambda key, context: '总行数' if key == 'count_label' else '1'
        headers=iter(['other header','送货单号'])
        query.record=lambda key,context: ({'name':next(headers),'role':25,'rect':[600,330,800,350]} if key=='header'
                                          else {'value':'001234','role':29,'rect':[600,350,800,375]})
        self.assertEqual(query.snapshot(None, None), {'count': 1, 'number': '001234'})
        self.assertEqual(driver.bridge.call.call_args[0][0], 'scroll')

    def test_binding_export_button_cannot_be_used_as_query(self):
        query=LocalQuery(Mock(),profile())
        query.read=lambda key,context:'导出明细'
        with self.assertRaises(WDMSError) as caught:
            query.guard_query_input(Mock())
        self.assertEqual(caught.exception.code,'MOYU_QUERY_BUTTON_UNCONFIRMED')

    def test_query_accelerator_caption_is_accepted(self):
        query=LocalQuery(Mock(),profile())
        query.read=lambda key,context:' 查 询(&F) '
        query.query_button(Mock())

    def test_wrong_column_is_not_interpreted_as_delivery_number(self):
        driver = Mock()
        query = LocalQuery(driver, profile())
        query.read = lambda key, context: '总行数' if key == 'count_label' else '1'
        query.record = lambda key, context: {'name':'合同号'}
        with self.assertRaises(WDMSError):
            query.snapshot(None, None)
        driver.bridge.call.assert_not_called()

    def test_other_numeric_cell_cannot_be_used_as_delivery_column(self):
        header={'role':25,'rect':[600,330,800,350]}
        with self.assertRaises(WDMSError):
            confirm_column(header,{'role':29,'rect':[800,350,900,375]})
        confirm_column(header,{'role':29,'rect':[600,350,800,375]})

    def test_inventory_count_label_is_not_delivery_row_count(self):
        query=LocalQuery(Mock(),profile())
        query.read=lambda key,context:'整箱数' if key=='count_label' else '0'
        with self.assertRaises(WDMSError) as caught:
            query.count(Mock())
        self.assertEqual(caught.exception.code,'MOYU_COUNT_LABEL_UNCONFIRMED')

    def test_guard_rejects_moved_window_and_changed_dpi(self):
        driver = Driver.__new__(Driver)
        driver.api = Mock()
        driver.u = Mock()
        root = profile()['wdms']['root']
        driver.api.metadata.return_value = {**root, 'rect': [1, 0, 901, 700]}
        with self.assertRaises(WDMSError):
            driver.guard(root)
        driver.api.metadata.return_value = root
        driver.u.GetDpiForWindow.return_value = 144
        with self.assertRaises(WDMSError):
            driver.guard(root)

    def test_hung_wdms_ghost_waits_without_reading_cached_or_foreign_controls(self):
        driver=Mock()
        driver.identity.return_value=profile()['wdms']['root']
        driver.u.IsHungAppWindow.return_value=True
        query=LocalQuery(driver,profile())
        self.assertEqual(query.signal(None,Mock()),{'busy':False,'responsive':False})
        driver.bridge.call.assert_not_called()
        driver.u.GetCursorPos.assert_not_called()

    def test_ghost_cleanup_is_waited_for_but_another_application_is_rejected(self):
        driver=Mock()
        root=profile()['wdms']['root']
        driver.identity.return_value=root
        driver.u.IsHungAppWindow.return_value=False
        driver.u.GetForegroundWindow.return_value=999
        driver.api.metadata.return_value={**root,'class_name':'Ghost'}
        query=LocalQuery(driver,profile())
        self.assertEqual(query.signal(None,Mock()),{'busy':False,'responsive':False})
        driver.api.metadata.return_value={**root,'class_name':'OtherApp'}
        with self.assertRaises(WDMSError) as caught:
            query.signal(None,Mock())
        self.assertEqual(caught.exception.code,'MOYU_FOREGROUND_CHANGED')

    def test_guard_allows_foreground_child_or_owned_window(self):
        driver = Driver.__new__(Driver)
        driver.api = Mock()
        driver.u = Mock()
        root = profile()['wdms']['root']
        driver.api.metadata.return_value = root
        driver.u.GetDpiForWindow.return_value = root['dpi']
        driver.u.GetForegroundWindow.return_value = 999
        driver.u.GetAncestor.side_effect = lambda hwnd, mode: root['hwnd'] if mode == 2 else 0
        driver.guard(root, foreground=True)
        driver.u.GetAncestor.side_effect = lambda hwnd, mode: root['hwnd'] if mode == 3 else 0
        driver.guard(root, foreground=True)
        driver.u.GetAncestor.side_effect = lambda hwnd, mode: 0
        with self.assertRaises(WDMSError) as caught:
            driver.guard(root, foreground=True)
        self.assertEqual(caught.exception.code, 'MOYU_FOREGROUND_CHANGED')

    def test_keyboard_partial_input_releases_without_retyping(self):
        driver = Driver.__new__(Driver)
        driver.guard = Mock()
        driver.input_guard = None
        driver.u = Mock()
        driver.u.GetAsyncKeyState.return_value = 0
        driver.u.SendInput.side_effect = [2, 2]
        with self.assertRaises(WDMSError):
            driver.chord(profile()['remote']['root'], 0x43, Mock())
        calls = driver.u.SendInput.call_args_list
        self.assertEqual(len(calls), 2)
        release = calls[1].args[1]
        self.assertTrue(all(release[i].ki.dwFlags & 2 for i in range(calls[1].args[0])))

    def test_cancel_during_final_guard_does_not_emit_input(self):
        driver = Driver.__new__(Driver)
        driver.input_guard = Mock(side_effect=WDMSError('TASK_CANCELLED', 'stop'))
        driver.u = Mock()
        with self.assertRaises(WDMSError):
            driver.chord(profile()['remote']['root'], 0x43, Mock())
        driver.u.SendInput.assert_not_called()


class MoyuEntryTests(unittest.TestCase):
    def test_read_helper_timeout_recreates_helper_without_repeating_request(self):
        from moyu_tools.bridge import Bridge
        bridge = Bridge.__new__(Bridge)
        bridge.log, bridge.started = None, True
        old = Mock()
        old.command, old.cwd = ['synthetic-helper'], 'synthetic-cwd'
        old.request.side_effect = WDMSError('WORKER_HARD_TIMEOUT', 'read timed out')
        bridge.worker = old
        bridge.close = Mock()
        with patch('moyu_tools.bridge.JsonLineWorker') as replacement:
            with self.assertRaises(WDMSError) as caught:
                bridge.call('read', {'point': [1, 2]})
            self.assertEqual(caught.exception.code, 'MOYU_READ_PENDING')
            replacement.return_value.request.assert_not_called()
        self.assertEqual(old.request.call_count, 1)

    def test_date_write_timeout_is_never_recreated_or_repeated(self):
        from moyu_tools.bridge import Bridge
        bridge = Bridge.__new__(Bridge)
        bridge.log, bridge.started = None, True
        bridge.worker = Mock()
        bridge.worker.request.side_effect = WDMSError('WORKER_HARD_TIMEOUT', 'unknown write state')
        with patch('moyu_tools.bridge.JsonLineWorker') as replacement:
            with self.assertRaises(WDMSError) as caught:
                bridge.call('date', {'value': '2025-01-01'})
            self.assertEqual(caught.exception.code, 'WORKER_HARD_TIMEOUT')
            replacement.assert_not_called()

    def test_worker_entry_calls_no_gui_or_old_login_routes(self):
        from scripts.MoyuTools import main
        with patch('moyu_tools.runtime.worker', return_value=2) as worker:
            self.assertEqual(main(['--worker', 'synthetic-request.json']), 2)
            worker.assert_called_once_with('synthetic-request.json')
        with self.assertRaises(SystemExit):
            main(['--native-login'])

    def test_bridge_refuses_native_operations_on_non_windows(self):
        from moyu_tools.bridge import Bridge
        with patch('moyu_tools.bridge.sys.platform', 'darwin'), self.assertRaises(WDMSError):
            Bridge()

    def test_new_modules_pass_existing_source_gate(self):
        from scripts.check_source import check_text
        base = Path(__file__).resolve().parents[1]
        paths = list((base/'moyu_tools').glob('*.py'))+[base/'scripts'/'MoyuTools.py']
        findings = [(str(p), f) for p in paths for f in check_text(p.read_text(encoding='utf-8'), str(p))]
        self.assertEqual(findings, [])
