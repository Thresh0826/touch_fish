import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from desktop_demo import login
from errors import WDMSError

class NativeLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/'login.jsonl'
        self.clock=[0.0]
        for patch in (mock.patch.object(login.time,'monotonic',side_effect=lambda:self.clock[0]),
                      mock.patch.object(login.time,'sleep',side_effect=lambda seconds:self.clock.__setitem__(0,self.clock[0]+seconds))):
            patch.start();self.addCleanup(patch.stop)

    def events(self):
        def node(hwnd,kind,rect):
            return {'event':'native_control','hwnd':hwnd,'class_name':f'WindowsForms10.{kind}.app.test','visible':True,'rect':rect}
        return [node(1,'EDIT',[80,0,200,20]),node(2,'EDIT',[80,30,200,50]),
                {'event':'native_text','hwnd':3,'class_name':'WindowsForms10.BUTTON.app.test','text':'登录(&L)','rect':[100,60,180,80]}]

    def api(self):
        return SimpleNamespace(user32=SimpleNamespace(GetWindowLongW=lambda hwnd,index:0x20 if hwnd==2 else 0))

    def test_unique_username_password_and_submit_mapping(self):
        mapped=login.map_login_controls(self.api(),self.events())
        self.assertEqual([mapped[k]['hwnd'] for k in ('username','password','submit')],[1,2,3])

    def test_authenticated_password_menu_with_mdi_is_home_marker(self):
        events=[{'event':'native_control','class_name':'WindowsForms10.MDICLIENT.app','visible':True},
                {'event':'native_text','class_name':'WindowsForms10.STATIC.app','text':'       用户密码修改','visible':True}]
        self.assertEqual(login.logged_in_home_markers(events),['       用户密码修改'])
        self.assertEqual(login.logged_in_home_markers(events[1:]),[])
        events[0]['visible']=False
        self.assertEqual(login.logged_in_home_markers(events),[])

    def test_multiple_password_fields_are_not_guessed(self):
        api=self.api();api.user32.GetWindowLongW=lambda hwnd,index:0x20
        with self.assertRaises(WDMSError) as caught:login.map_login_controls(api,self.events())
        self.assertEqual(caught.exception.code,'LOGIN_PASSWORD_CONTROL_UNCONFIRMED')

    def test_ambiguous_username_is_not_filled(self):
        events=self.events();events.append({**events[0],'hwnd':4})
        with self.assertRaises(WDMSError) as caught:login.map_login_controls(self.api(),events)
        self.assertEqual(caught.exception.code,'LOGIN_USERNAME_CONTROL_UNCONFIRMED')

    def test_duplicate_submit_buttons_stop_login(self):
        events=self.events();events.append({**events[-1],'hwnd':4})
        with self.assertRaises(WDMSError) as caught:login.map_login_controls(self.api(),events)
        self.assertEqual(caught.exception.code,'LOGIN_SUBMIT_CONTROL_UNCONFIRMED')

    def run_child(self, home_marker=True, mapping_error=False, readiness=None, close_after=0):
        settings=SimpleNamespace(wdms_exe='App.exe',username='test-user',credential_target='test-target')
        roots=[{'hwnd':10,'pid':7},{'hwnd':20,'pid':7}]
        api=SimpleNamespace(user32=SimpleNamespace(IsWindow=lambda hwnd:hwnd==10,
                            IsWindowVisible=lambda hwnd:self.clock[0]<close_after,IsWindowEnabled=lambda hwnd:True),health=mock.Mock(),
                            probe_login_ready=mock.Mock(side_effect=readiness),click_query_input=mock.Mock(),scan_times=[],
                            metadata=lambda hwnd:{'hwnd':hwnd,'pid':7,'visible':True})
        credentials=mock.Mock();credentials.read.return_value=('test-user','private-password-fixture')
        def scan(api,root,log,**kwargs):
            api.scan_times.append(self.clock[0])
            login.probe.record(log,'native_scan_complete')
            if root['hwnd']==20 and home_marker:
                login.probe.record(log,'native_text',text='在库管理')
        controls={key:{'hwnd':value} for key,value in (('username',1),('password',2),('submit',3))}
        with mock.patch.object(login,'load_settings',return_value=settings),mock.patch.object(login,'validate_executable',return_value='App.exe'),mock.patch.object(login.probe,'inspect_environment',return_value={}),mock.patch.object(login.probe,'require_usable_environment'),mock.patch('psutil.process_iter',return_value=[]),mock.patch.object(login.subprocess,'Popen'),mock.patch.object(login,'LoginNativeWindows',return_value=api),mock.patch.object(login,'find_stage_window',side_effect=lambda api,stage,log:roots[0] if stage=='login' else roots[1]),mock.patch.object(login,'pid_matches',return_value=True),mock.patch.object(login.probe,'check_native_controls',side_effect=scan),mock.patch.object(login,'map_login_controls',side_effect=WDMSError('LOGIN_PASSWORD_CONTROL_UNCONFIRMED','unconfirmed') if mapping_error else None,return_value=controls),mock.patch.object(login,'WindowsCredentials',return_value=credentials),mock.patch.object(login,'click_department_labels'),mock.patch.object(login,'prepare_fields'),mock.patch.object(login,'wait_submit_enabled'):
            code=login.child(SimpleNamespace(log=str(self.log)))
        return code,api,credentials

    def test_slow_network_login_in_real_child_submits_once_and_passes_after_45_seconds(self):
        code,api,credentials=self.run_child(close_after=60)
        self.assertEqual(code,0)
        self.assertGreaterEqual(self.clock[0],60)
        api.click_query_input.assert_called_once()
        credentials.read.assert_called_once()
        events=login.probe.read_events(self.log)
        self.assertEqual(sum(event['event']=='login_submitted' for event in events),1)
        self.assertEqual(events[-1]['event'],'automatic_login_complete')

    def test_login_submits_once_and_credentials_are_absent_from_logs(self):
        code,api,credentials=self.run_child()
        self.assertEqual(code,0)
        api.click_query_input.assert_called_once()
        self.assertNotIn('private-password-fixture',self.log.read_text(encoding='utf-8'))
        self.assertTrue(any(e['event']=='automatic_login_complete' for e in login.probe.read_events(self.log)))

    def test_real_login_child_waits_for_ready_before_scanning_or_using_credentials(self):
        def ready(root,log):
            if self.clock[0]<22:
                raise WDMSError('WDMS_UNRESPONSIVE','synthetic cold startup')
        code,api,credentials=self.run_child(readiness=ready)
        self.assertEqual(code,0)
        self.assertGreaterEqual(api.scan_times[0],24)
        credentials.read.assert_called_once()
        api.click_query_input.assert_called_once()
        events=login.probe.read_events(self.log)
        self.assertTrue(any(event['event']=='login_window_ui_recovered' for event in events))
        self.assertEqual(events[-1]['event'],'automatic_login_complete')

    def test_window_disappearance_alone_does_not_prove_login_success(self):
        code,api,credentials=self.run_child(home_marker=False)
        self.assertEqual(code,2)
        api.click_query_input.assert_called_once()
        self.assertEqual(login.probe.read_events(self.log)[-1]['error_code'],'LOGIN_HOME_MARKER_UNCONFIRMED')

    def test_unconfirmed_fields_do_not_read_credentials_or_submit(self):
        code,api,credentials=self.run_child(mapping_error=True)
        self.assertEqual(code,2)
        credentials.read.assert_not_called()
        api.click_query_input.assert_not_called()

    def test_spaced_chinese_login_button_is_recognized(self):
        events=self.events();events[-1]['text']=' 登 录'
        controls=login.map_login_controls(self.api(),events)
        self.assertEqual(controls['submit']['hwnd'],3)
        self.assertEqual(login.normalized('密　码'),'密码')

    def test_normalization_does_not_resolve_duplicate_login_buttons(self):
        events=self.events();events.append({**events[-1],'hwnd':4,'text':'登 录'})
        with self.assertRaises(WDMSError) as caught:login.map_login_controls(self.api(),events)
        self.assertEqual(caught.exception.code,'LOGIN_SUBMIT_CONTROL_UNCONFIRMED')

    def test_fields_receive_focus_before_values_are_written(self):
        steps=[]
        api=SimpleNamespace(click_query_input=lambda root,hwnd,log:steps.append(('click',hwnd)))
        controls={'username':{'hwnd':1},'password':{'hwnd':2}}
        with mock.patch.object(login,'wait_field_focus',side_effect=lambda api,root,item,log:steps.append(('focus',item['hwnd']))),mock.patch.object(login,'set_field',side_effect=lambda api,item,value,pid:steps.append(('fill',item['hwnd']))):
            login.prepare_fields(api,{'pid':7},controls,'user','private-fixture',self.log)
        self.assertEqual(steps,[('click',1),('focus',1),('fill',1),('click',2),('focus',2),('fill',2)])
        self.assertNotIn('private-fixture',self.log.read_text(encoding='utf-8'))

    def test_login_waits_for_button_to_become_enabled(self):
        clock=[0.0]
        api=SimpleNamespace(metadata=lambda hwnd:{'pid':7,'class_name':'BUTTON','visible':True},user32=SimpleNamespace(IsWindowEnabled=lambda hwnd:clock[0]>=0.3))
        with mock.patch.object(login.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(login.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            login.wait_submit_enabled(api,{'pid':7},{'submit':{'hwnd':3,'class_name':'BUTTON'}},[],self.log,timeout=1)
        self.assertGreaterEqual(clock[0],0.3)
        self.assertEqual(login.probe.read_events(self.log)[-1]['event'],'login_submit_ready')

    def test_disabled_button_is_not_bypassed(self):
        clock=[0.0]
        api=SimpleNamespace(metadata=lambda hwnd:{'pid':7,'class_name':'BUTTON','visible':True},user32=SimpleNamespace(IsWindowEnabled=lambda hwnd:False))
        with mock.patch.object(login.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(login.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            with self.assertRaises(WDMSError) as caught:
                login.wait_submit_enabled(api,{'pid':7},{'submit':{'hwnd':3,'class_name':'BUTTON'}},[],self.log,timeout=1)
        self.assertEqual(caught.exception.code,'LOGIN_SUBMIT_DISABLED')


    def label_api(self,hit):
        api=object.__new__(login.LoginNativeWindows)
        api.metadata=lambda hwnd:{'pid':7,'class_name':'WindowsForms10.STATIC.app','visible':True,'rect':[10,20,50,40]}
        def cursor(pointer):
            pointer._obj.x=30;pointer._obj.y=30
            return True
        api.user32=SimpleNamespace(SetForegroundWindow=lambda hwnd:True,GetForegroundWindow=lambda:100,
            IsWindowEnabled=lambda hwnd:True,WindowFromPoint=lambda point:hit,
            GetAncestor=lambda hwnd,flags:hwnd,GetParent=lambda hwnd:100,
            ScreenToClient=lambda hwnd,pointer:True,ChildWindowFromPointEx=lambda hwnd,point,flags:10,
            GetAsyncKeyState=lambda key:0,SetCursorPos=lambda x,y:True,GetCursorPos=cursor,
            SendInput=mock.Mock(return_value=1),SendMessageTimeoutW=mock.Mock(return_value=1))
        return api

    def test_transparent_label_parent_hit_sends_one_mouse_click(self):
        api=self.label_api(100)
        with mock.patch.object(login.time,'sleep') as sleep:
            api.click_query_input({'hwnd':100,'pid':7},10,self.log)
        self.assertEqual(api.user32.SendMessageTimeoutW.call_count,2)
        self.assertEqual([c.args[0] for c in api.user32.SendMessageTimeoutW.call_args_list],[10,10])
        self.assertEqual([c.args[1] for c in api.user32.SendMessageTimeoutW.call_args_list],[0x0201,0x0202])
        self.assertEqual(sleep.call_args_list,[mock.call(0.2),mock.call(0.12)])

    def test_mouse_released_if_hold_is_interrupted(self):
        api=self.label_api(10)
        with mock.patch.object(login.time,'sleep',side_effect=[None,RuntimeError('interrupted')]):
            with self.assertRaises(RuntimeError):
                api.click_query_input({'hwnd':100,'pid':7},10,self.log)
        self.assertEqual([c.args[1] for c in api.user32.SendMessageTimeoutW.call_args_list],[0x0201,0x0202])

    def test_failed_mouse_down_is_not_retried(self):
        api=self.label_api(10);api.user32.SendMessageTimeoutW.side_effect=[0,1]
        with mock.patch.object(login.time,'sleep'):
            with self.assertRaises(WDMSError):
                api.click_query_input({'hwnd':100,'pid':7},10,self.log)
        self.assertEqual([c.args[1] for c in api.user32.SendMessageTimeoutW.call_args_list],[0x0201,0x0202])

    def test_cold_label_handler_gets_a_bounded_longer_budget(self):
        api=self.label_api(10)
        def send(hwnd,message,buttons,position,flags,timeout,result):
            self.clock[0] += 0.8
            return int(message == 0x0202 or timeout >= 800)
        api.user32.SendMessageTimeoutW.side_effect=send
        self.assertTrue(api.click_query_input({'hwnd':100,'pid':7},10,self.log))
        calls=api.user32.SendMessageTimeoutW.call_args_list
        self.assertEqual([call.args[1] for call in calls],[0x0201,0x0202])
        self.assertEqual([call.args[5] for call in calls],[2500,2500])
        messages=[event for event in login.probe.read_events(self.log) if event['event']=='label_mouse_message']
        self.assertEqual(messages[0]['elapsed_ms'],800)
        self.assertEqual(messages[0]['winerror'],0)

    def test_mouse_error_is_captured_before_release_overwrites_it(self):
        api=self.label_api(10)
        api.user32.SendMessageTimeoutW.side_effect=[0,0]
        with mock.patch.object(login.ctypes,'set_last_error',create=True) as reset, \
             mock.patch.object(login.ctypes,'get_last_error',side_effect=[1460,5],create=True):
            with self.assertRaises(WDMSError) as caught:
                api.click_query_input({'hwnd':100,'pid':7},10,self.log)
        self.assertIn('WinError=1460',caught.exception.message)
        self.assertEqual(reset.call_args_list,[mock.call(0),mock.call(0)])
        messages=[event for event in login.probe.read_events(self.log) if event['event']=='label_mouse_message']
        self.assertEqual([event['winerror'] for event in messages],[1460,5])
        self.assertEqual(api.user32.SendMessageTimeoutW.call_count,2)

    def test_label_message_logs_are_written_only_after_release(self):
        api=self.label_api(10)
        original=login.probe.record
        def emit(path,event,**fields):
            if event=='label_mouse_message':
                self.assertEqual([call.args[1] for call in api.user32.SendMessageTimeoutW.call_args_list],
                                 [0x0201,0x0202])
            return original(path,event,**fields)
        with mock.patch.object(login.probe,'record',side_effect=emit):
            self.assertTrue(api.click_query_input({'hwnd':100,'pid':7},10,self.log))

    def test_saved_selection_restores_during_intent_log_without_press(self):
        api=self.label_api(10)
        selected=[False]
        original=login.probe.record
        def emit(path,event,**fields):
            if event=='before_label_mouse_input':selected[0]=True
            return original(path,event,**fields)
        with mock.patch.object(login.probe,'record',side_effect=emit):
            clicked=api.click_query_input({'hwnd':100,'pid':7},10,self.log,before_press=lambda:not selected[0])
        self.assertFalse(clicked)
        api.user32.SendMessageTimeoutW.assert_not_called()

    def test_log_failure_after_input_does_not_mask_native_failure(self):
        api=self.label_api(10)
        api.user32.SendMessageTimeoutW.side_effect=[0,1]
        original=login.probe.record
        def emit(path,event,**fields):
            if event=='label_mouse_message':raise PermissionError('synthetic occupied log')
            return original(path,event,**fields)
        with mock.patch.object(login.probe,'record',side_effect=emit):
            with self.assertRaises(WDMSError) as caught:
                api.click_query_input({'hwnd':100,'pid':7},10,self.log)
        self.assertEqual(caught.exception.code,'LOGIN_INPUT_FAILED')
        self.assertEqual(api.user32.SendMessageTimeoutW.call_count,2)

    def test_department_becomes_selected_during_hover_prevents_press(self):
        api=self.label_api(10)
        permitted=lambda:self.clock[0]<0.1
        clicked=api.click_query_input({'hwnd':100,'pid':7},10,self.log,before_press=permitted)
        self.assertFalse(clicked)
        api.user32.SendMessageTimeoutW.assert_not_called()

    def test_label_covered_by_other_window_is_not_clicked(self):
        api=self.label_api(999)
        with self.assertRaises(WDMSError) as caught:
            api.click_query_input({'hwnd':100,'pid':7},10,self.log)
        self.assertEqual(caught.exception.code,'LOGIN_LABEL_OBSCURED')
        api.user32.SendInput.assert_not_called()
        api.user32.SendMessageTimeoutW.assert_not_called()

    def department_events(self):
        labels=[{'event':'native_text','hwnd':10+i,'class_name':'WindowsForms10.STATIC.app','text':name,'visible':True,'rect':[0,i*30,40,i*30+20]} for i,name in enumerate(('管理部','仓储部','运输部'))]
        return labels+[{'event':'native_control','hwnd':20+i,'class_name':'WindowsForms10.COMBOBOX.app','visible':True,'rect':[45,i*30,150,i*30+20]} for i in range(3)]

    def department_api(self,events,enabled=(),activate=True):
        nodes={e['hwnd']:e for e in events}
        states=set(enabled)
        def click(root,hwnd,log,before_press=None):
            if before_press is not None and not before_press():return False
            if activate:states.add(hwnd+10)
            return True
        return SimpleNamespace(metadata=lambda hwnd:{**nodes[hwnd],'pid':7},
            user32=SimpleNamespace(IsWindowEnabled=lambda hwnd:hwnd in states),
            probe_login_ready=mock.Mock(),click_query_input=mock.Mock(side_effect=click))

    def test_cold_unresponsive_window_cannot_make_cached_departments_stable(self):
        events=self.department_events();api=self.department_api(events,enabled=(20,21,22))
        reads=[]
        def ready(root,log):
            if self.clock[0]<22:
                raise WDMSError('WDMS_UNRESPONSIVE','synthetic cold startup')
        def enabled(hwnd):
            reads.append(self.clock[0]);return True
        api.probe_login_ready.side_effect=ready
        api.user32.IsWindowEnabled=enabled
        login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertGreaterEqual(min(reads),22)
        self.assertGreaterEqual(self.clock[0],27)
        api.click_query_input.assert_not_called()
        self.assertIn('department_ui_recovered',[event['event'] for event in login.probe.read_events(self.log)])

    def test_cold_start_never_clicks_an_unselected_label_before_ui_ready(self):
        events=self.department_events();api=self.department_api(events)
        click=api.click_query_input.side_effect
        def ready(root,log):
            if self.clock[0]<22:
                raise WDMSError('WDMS_HEALTH_CHECK_FAILED','synthetic cold startup')
        def checked_click(*args,**kwargs):
            self.assertGreaterEqual(self.clock[0],27)
            return click(*args,**kwargs)
        api.probe_login_ready.side_effect=ready
        api.click_query_input.side_effect=checked_click
        login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertEqual(api.click_query_input.call_count,3)

    def test_persistent_unresponsive_startup_stops_without_reading_or_clicking(self):
        events=self.department_events();api=self.department_api(events)
        api.probe_login_ready.side_effect=WDMSError('WDMS_UNRESPONSIVE','synthetic busy')
        enabled=mock.Mock(return_value=False);api.user32.IsWindowEnabled=enabled
        with self.assertRaises(WDMSError) as caught:
            login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertEqual(caught.exception.code,'LOGIN_WINDOW_NOT_READY')
        self.assertAlmostEqual(self.clock[0],90)
        enabled.assert_not_called();api.click_query_input.assert_not_called()

    def test_losing_responsiveness_restarts_department_observation(self):
        def ready():
            if 2<=self.clock[0]<7:
                raise WDMSError('WDMS_HEALTH_CHECK_FAILED','synthetic interrupted startup')
        states=login.wait_department_states(lambda:(True,True,True),self.log,ready=ready)
        self.assertEqual(states,(True,True,True))
        self.assertGreaterEqual(self.clock[0],12)

    def test_startup_access_denied_is_not_a_recoverable_slow_computer(self):
        events=self.department_events();api=self.department_api(events)
        api.probe_login_ready.side_effect=WDMSError('WDMS_ACCESS_DENIED','synthetic denied')
        with self.assertRaises(WDMSError) as caught:
            login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertEqual(caught.exception.code,'WDMS_ACCESS_DENIED')
        api.probe_login_ready.assert_called_once()
        api.click_query_input.assert_not_called()
        self.assertEqual(self.clock[0],0)

    def test_late_successful_probe_does_not_extend_the_wait_deadline(self):
        read=mock.Mock(return_value=(True,True,True))
        def ready():self.clock[0]+=1
        with self.assertRaises(WDMSError) as caught:
            login.wait_department_states(read,self.log,timeout=1,ready=ready)
        self.assertEqual(caught.exception.code,'LOGIN_WINDOW_NOT_READY')
        read.assert_not_called()

    def test_prescan_and_department_wait_share_one_startup_deadline(self):
        events=self.department_events();api=self.department_api(events)
        root={'hwnd':100,'pid':7}
        login.wait_login_window_ready(api,root,self.log,deadline=6)
        self.clock[0]+=3
        with self.assertRaises(WDMSError):
            login.click_department_labels(api,root,events,self.log,startup_deadline=6)
        self.assertAlmostEqual(self.clock[0],6)
        api.click_query_input.assert_not_called()

    def test_already_ready_saved_departments_do_not_use_the_whole_startup_budget(self):
        events=self.department_events();api=self.department_api(events,enabled=(20,21,22))
        login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertLess(self.clock[0],10)
        api.click_query_input.assert_not_called()

    def test_ready_probe_is_one_native_health_attempt_without_nested_retries(self):
        api=object.__new__(login.LoginNativeWindows)
        api.metadata=lambda hwnd:{'pid':7,'visible':True,'class_name':'root'}
        with mock.patch.object(login.probe.NativeWindows,'health',side_effect=WDMSError('WDMS_UNRESPONSIVE','synthetic busy')) as health:
            with self.assertRaises(WDMSError):
                api.probe_login_ready({'hwnd':100,'pid':7,'class_name':'root'},self.log)
        health.assert_called_once()
        self.assertEqual(self.clock[0],0)

    def test_ready_probe_rejects_changed_login_identity_before_health(self):
        api=object.__new__(login.LoginNativeWindows)
        api.metadata=lambda hwnd:{'pid':8,'visible':True,'class_name':'root'}
        with mock.patch.object(login.probe.NativeWindows,'health') as health:
            with self.assertRaises(WDMSError) as caught:
                api.probe_login_ready({'hwnd':100,'pid':7,'class_name':'root'},self.log)
        self.assertEqual(caught.exception.code,'LOGIN_WINDOW_CHANGED')
        health.assert_not_called()

    def test_department_labels_are_clicked_once_each(self):
        events=self.department_events()
        api=self.department_api(events)
        click=api.click_query_input
        root={'hwnd':100,'pid':7}
        login.click_department_labels(api,root,events,self.log)
        self.assertEqual([c.args for c in click.call_args_list],[(root,10,self.log),(root,11,self.log),(root,12,self.log)])
        self.assertEqual(len([e for e in login.probe.read_events(self.log) if e['event']=='department_label_clicked']),3)

    def test_remembered_all_selected_departments_are_never_clicked(self):
        events=self.department_events();api=self.department_api(events,enabled=(20,21,22))
        login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        api.click_query_input.assert_not_called()
        self.assertEqual(login.probe.read_events(self.log)[-1]['event'],'all_departments_selected')

    def test_only_unselected_department_is_clicked(self):
        events=self.department_events();api=self.department_api(events,enabled=(20,22))
        root={'hwnd':100,'pid':7}
        login.click_department_labels(api,root,events,self.log)
        api.click_query_input.assert_called_once()
        self.assertEqual(api.click_query_input.call_args.args,(root,11,self.log))

    def test_saved_departments_restore_after_window_appears_without_clicks(self):
        events=self.department_events();api=self.department_api(events)
        api.user32.IsWindowEnabled=lambda hwnd:self.clock[0]>=2
        login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        api.click_query_input.assert_not_called()
        states=[e['states'] for e in login.probe.read_events(self.log) if e['event']=='department_settle_state']
        self.assertEqual(states[:2],[[False,False,False],[True,True,True]])

    def test_unstable_departments_stop_without_clicking(self):
        events=self.department_events();api=self.department_api(events)
        api.user32.IsWindowEnabled=lambda hwnd:bool(int(self.clock[0])%2)
        with self.assertRaises(WDMSError) as caught:
            login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertEqual(caught.exception.code,'LOGIN_DEPARTMENT_STATE_UNSTABLE')
        api.click_query_input.assert_not_called()

    def test_slow_initial_restore_resets_stability_timer(self):
        restored=lambda:(self.clock[0]>=4.5,)*3
        states=login.wait_department_states(restored,self.log)
        self.assertEqual(states,(True,True,True))
        self.assertGreaterEqual(self.clock[0],6.5)

    def test_click_that_does_not_enable_combo_stops_without_retry(self):
        events=self.department_events();api=self.department_api(events,activate=False)
        clock=[0.0]
        with mock.patch.object(login.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(login.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            with self.assertRaises(WDMSError) as caught:
                login.click_department_labels(api,{'hwnd':100,'pid':7},events,self.log)
        self.assertEqual(caught.exception.code,'LOGIN_DEPARTMENT_SELECTION_UNCONFIRMED')
        api.click_query_input.assert_called_once()

    def test_missing_department_combo_stops_before_any_click(self):
        events=self.department_events()[:-1];api=self.department_api(events)
        with self.assertRaises(WDMSError):login.click_department_labels(api,{'pid':7},events,self.log)
        api.click_query_input.assert_not_called()

    def test_missing_label_prevents_all_department_clicks(self):
        events=[e for e in self.department_events() if e.get('text')!='运输部']
        click=mock.Mock()
        api=SimpleNamespace(click_query_input=click)
        with self.assertRaises(WDMSError):login.click_department_labels(api,{'pid':7},events,self.log)
        click.assert_not_called()

    def test_department_label_from_changed_process_is_not_clicked(self):
        events=self.department_events()
        click=mock.Mock()
        api=SimpleNamespace(metadata=lambda hwnd:{'pid':99,'class_name':'WindowsForms10.STATIC.app','visible':True},
                            probe_login_ready=mock.Mock(),click_query_input=click)
        with self.assertRaises(WDMSError):login.click_department_labels(api,{'pid':7},events,self.log)
        click.assert_not_called()

class ExistingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/'login.jsonl'
        self.log.touch()
        self.root={'hwnd':100,'pid':7,'class_name':'WindowsForms10.Window.8.root',
                   'rect':[0,0,900,700],'visible':True,'minimized':False}
        self.link={'hwnd':10,'pid':7,'class_name':'WindowsForms10.STATIC.link',
                   'rect':[20,100,180,130],'visible':True,'event':'native_text','text':'在库管理'}
        self.group={'hwnd':20,'pid':7,'class_name':'WindowsForms10.Window.8.sidebar',
                    'rect':[0,80,200,700],'visible':True}
        self.api=mock.Mock()
        self.api.metadata.side_effect=lambda hwnd:dict(self.root if hwnd==100 else self.link)
        self.api.user32.IsWindowEnabled.return_value=True
        self.missing=WDMSError('STARTUP_WINDOW_NOT_UNIQUE','需要唯一窗口，当前找到 0 个。')

    def confirm(self, events=None, visible=True, page=False, pid_matches=True, end_login=False):
        from desktop_demo import navigation
        events=[self.link] if events is None else events
        geometry={'visible_rect':self.link['rect'] if visible else None,
                  'chain':[self.link,self.group,self.root]}
        windows=[self.root,self.root if end_login else self.missing]
        def scan(api,root,log,**kwargs):
            login.probe.record(log,'native_scan_complete')
        with mock.patch.object(navigation,'NavigationNativeWindows',return_value=self.api), \
             mock.patch.object(login,'find_stage_window',side_effect=windows), \
             mock.patch.object(login,'pid_matches',return_value=pid_matches), \
             mock.patch.object(login.probe,'check_native_controls',side_effect=scan), \
             mock.patch.object(navigation,'supplement_navigation_captions',return_value=events), \
             mock.patch.object(navigation,'confirm_inventory_page',return_value=page), \
             mock.patch.object(navigation,'inspect_geometry',return_value=geometry), \
             mock.patch.object(navigation,'navigation_caption',side_effect=lambda api,item,log:'企业管理' if item['hwnd']==20 else self.link['text']), \
             mock.patch.object(login,'WindowsCredentials') as credentials:
            try:
                return login.confirm_existing_session('SyntheticWDMS.exe',7,self.log)
            finally:
                credentials.assert_not_called()
                self.api.user32.SetForegroundWindow.assert_not_called()
                self.api.user32.SendInput.assert_not_called()
                self.api.click_query_input.assert_not_called()

    def test_confirmed_logged_in_sidebar_is_reused_without_input(self):
        self.assertEqual(self.confirm(),0)
        events=login.probe.read_events(self.log)
        self.assertEqual(events[-1]['event'],'automatic_login_complete')
        self.assertTrue(events[-1]['session_reused']);self.assertFalse(events[-1]['login_performed'])
        self.assertEqual(events[-2]['evidence'],'visible_home_marker')
        self.assertFalse(events[-2]['account_independently_verified'])

    def test_existing_query_page_is_reused_without_navigation(self):
        self.assertEqual(self.confirm(events=[],page=True),0)
        self.assertEqual(login.probe.read_events(self.log)[-2]['evidence'],'inventory_page')

    def test_enterprise_home_requires_employee_link_parent_relationship(self):
        self.link['text']='员工与用户'
        self.assertEqual(self.confirm(),0)
        self.assertEqual(login.probe.read_events(self.log)[-2]['evidence'],'enterprise_sidebar')

    def test_clipped_marker_unknown_page_wrong_process_and_disabled_root_are_rejected(self):
        for options in ({'visible':False},{'events':[]},{'pid_matches':False}):
            with self.subTest(options=options):
                with self.assertRaises(WDMSError) as caught:self.confirm(**options)
                self.assertEqual(caught.exception.code,'LOGIN_EXISTING_SESSION_UNCONFIRMED')
        self.api.user32.IsWindowEnabled.return_value=False
        with self.assertRaises(WDMSError):self.confirm()
        self.assertFalse(any(event['event']=='automatic_login_complete' for event in login.probe.read_events(self.log)))

    def test_new_login_window_at_end_prevents_session_reuse(self):
        with self.assertRaises(WDMSError) as caught:self.confirm(end_login=True)
        self.assertEqual(caught.exception.code,'LOGIN_EXISTING_SESSION_UNCONFIRMED')

    def test_existing_running_client_reuse_is_explicit_and_unit_login_keeps_old_behavior(self):
        settings=SimpleNamespace(wdms_exe='SyntheticWDMS.exe')
        process=SimpleNamespace(info={'pid':7,'exe':'SyntheticWDMS.exe'})
        for reuse in (False,True):
            with self.subTest(reuse=reuse):
                with mock.patch.object(login,'load_settings',return_value=settings), \
                     mock.patch.object(login,'validate_executable',return_value=settings.wdms_exe), \
                     mock.patch.object(login.probe,'inspect_environment'), \
                     mock.patch.object(login.probe,'require_usable_environment'), \
                     mock.patch('psutil.process_iter',return_value=[process]), \
                     mock.patch.object(login,'LoginNativeWindows',return_value=self.api), \
                     mock.patch.object(login,'find_stage_window',side_effect=self.missing), \
                     mock.patch.object(login,'confirm_existing_session',return_value=0) as confirm, \
                     mock.patch.object(login.subprocess,'Popen') as launch, \
                     mock.patch.object(login,'WindowsCredentials') as credentials:
                    code=login.child(SimpleNamespace(log=str(self.log),reuse_session=reuse))
                self.assertEqual(code,0 if reuse else 2)
                if reuse:confirm.assert_called_once_with(settings.wdms_exe,7,self.log)
                else:confirm.assert_not_called()
                launch.assert_not_called();credentials.assert_not_called()

    def test_reuse_flag_reaches_supervised_login_child(self):
        with mock.patch.object(login.probe,'supervise',return_value=2) as supervise:
            self.assertEqual(login.main(['--out',self.temp.name,'--reuse-session']),2)
        self.assertIn('--reuse-session',supervise.call_args.args[0])


if __name__=='__main__':unittest.main()
