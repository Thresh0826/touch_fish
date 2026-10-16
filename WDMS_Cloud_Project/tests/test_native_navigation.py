import ctypes
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from desktop_demo import navigation as nav
from errors import NativeTextReadError, WDMSError


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/'navigation.jsonl'
        self.clock=[0.0]
        for p in (mock.patch.object(nav.time,'monotonic',side_effect=lambda:self.clock[0]),mock.patch.object(nav.time,'sleep',side_effect=lambda s:self.clock.__setitem__(0,self.clock[0]+s)),
                  mock.patch.object(ctypes,'set_last_error',create=True),
                  mock.patch.object(ctypes,'get_last_error',return_value=0,create=True)):
            p.start();self.addCleanup(p.stop)
        self.root={'hwnd':100,'pid':7,'class_name':'WindowsForms10.Window.8.root',
                   'visible':True,'minimized':False,'rect':[-100,-100,1000,1000]}

    def target(self,name,hwnd=1):
        node={'event':'native_control','hwnd':hwnd,'pid':7,'class_name':'WindowsForms10.STATIC.app','visible':True,'rect':[0,0,80,20]}
        return [node,{**node,'event':'native_text','text':name}]

    def page(self):
        result=self.target('在库商品检索',10)
        for hwnd,name,kind,rect in ((11,'货主','STATIC',[0,30,40,50]),(12,'','COMBOBOX',[45,30,160,50]),(13,'查询(&F)','BUTTON',[200,30,260,50]),(14,'整件数：','STATIC',[0,100,50,120]),(15,'散件数：','STATIC',[100,100,150,120])):
            item={'event':'native_control','hwnd':hwnd,'pid':7,'class_name':f'WindowsForms10.{kind}.app','visible':True,'rect':rect}
            result.extend([item,{**item,'event':'native_text','text':name}])
        return result

    def api(self,views):
        nodes={e['hwnd']:e for view in views for e in view if e['event']=='native_control'}
        names={e['hwnd']:e['text'] for view in views for e in view if e['event']=='native_text'}
        nodes[100]={'hwnd':100,'pid':7,'class_name':'WindowsForms10.Window.8.root',
                    'rect':[-100,-100,1000,1000],'visible':True}
        def caption(hwnd,message,size,address,flags,timeout,result):
            text=names.get(hwnd,'');buffer=ctypes.create_unicode_buffer(text)
            ctypes.memmove(address,buffer,ctypes.sizeof(buffer));result._obj.value=len(text)
            return True
        def set_view(view):
            nodes.update({e['hwnd']:e for e in view if e['event']=='native_control'})
            names.update({e['hwnd']:e['text'] for e in view if e['event']=='native_text'})
        return SimpleNamespace(metadata=lambda hwnd:nodes[hwnd],read_label_text=lambda data,log:names[data['hwnd']],
            client_screen_rect=lambda hwnd:nodes[hwnd].get('client_rect',nodes[hwnd]['rect']),
            user32=SimpleNamespace(IsWindowEnabled=lambda hwnd:True,
                GetParent=lambda hwnd:0 if hwnd==100 else nodes[hwnd].get('parent_hwnd',100),
                SendMessageTimeoutW=caption),health=mock.Mock(),set_view=set_view,
            click_navigation_input=mock.Mock(side_effect=lambda root,target,log,before_press:before_press()))

    def test_three_steps_once_then_confirm_empty_query_page(self):
        views=[self.target('在库管理',1),self.target('在库查询',2),self.target('在库商品检索',3),self.page()]
        api=self.api(views);scan=mock.Mock(side_effect=views)
        nav.navigate(api,self.root,self.log,scan)
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[1,2,3])
        complete=nav.probe.read_events(self.log)[-1]
        self.assertEqual(complete['event'],'inventory_page_ready')
        self.assertFalse(complete['query_performed'])

    def timeout_error(self, winerror=1460, code='NATIVE_TEXT_READ_FAILED'):
        return NativeTextReadError(code, f'合成页面标签读取失败，WinError={winerror}',
                                   winerror=winerror, hwnd=90,
                                   class_name='WindowsForms10.STATIC.synthetic')

    def test_open_page_timeout_restarts_read_without_repeating_any_menu_click(self):
        # Replay the onsite sequence: all three clicks succeed, then a label
        # read 0.4 seconds after opening the page times out with WinError 1460.
        views=[self.target('在库管理',1),self.target('在库查询',2),
               self.target('在库商品检索',3),self.page()]
        api=self.api(views)
        incomplete=[e for e in views[-1] if e['hwnd']!=15]
        scan=mock.Mock(side_effect=views[:3]+[self.timeout_error(),incomplete,views[-1]])
        nav.navigate(api,self.root,self.log,scan)
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[1,2,3])
        self.assertEqual([c.args[0] for c in scan.call_args_list][-3:],['step_3_after']*3)
        events=nav.probe.read_events(self.log)
        self.assertEqual(sum(e['event']=='inventory_page_ready' for e in events),1)
        deferred=next(e for e in events if e['event']=='page_scan_deferred')
        self.assertEqual((deferred['winerror'],deferred['hwnd']),(1460,90))
        self.assertTrue(deferred['read_only']);self.assertFalse(deferred['input_repeated'])
        self.assertTrue(any(e['event']=='page_scan_recovered' for e in events))

    def test_sustained_page_text_timeout_stops_with_original_error_without_reclick(self):
        link=self.target('在库商品检索',3);api=self.api([link]);error=self.timeout_error()
        scan=mock.Mock(side_effect=lambda stage:link if stage=='initial' else self.raise_error(error))
        with self.assertRaises(NativeTextReadError) as caught:
            nav.navigate(api,self.root,self.log,scan)
        self.assertIs(caught.exception,error)
        self.assertLessEqual(self.clock[0],25.4)
        self.assertEqual(api.click_navigation_input.call_count,1)
        events=nav.probe.read_events(self.log)
        self.assertFalse(any(e['event']=='inventory_page_ready' for e in events))
        self.assertEqual(events[-1]['event'],'page_scan_wait_exhausted')

    @staticmethod
    def raise_error(error):
        raise error

    def test_non_timeout_or_unstructured_errors_do_not_enter_read_wait(self):
        api=self.api([])
        for error in (self.timeout_error(5),self.timeout_error(0),
                      WDMSError('NATIVE_TEXT_READ_FAILED','WinError=1460'),
                      WDMSError('NATIVE_TEXT_TOO_LONG','合成文字被截断'),
                      WDMSError('WDMS_HEALTH_CHECK_FAILED','WinError=1460')):
            with self.subTest(code=error.code):
                scan=mock.Mock(side_effect=error)
                with self.assertRaises(WDMSError) as caught:
                    nav.scan_page_state(api,self.root,self.log,scan,'initial',10)
                self.assertIs(caught.exception,error);self.assertEqual(scan.call_count,1)
        api.click_navigation_input.assert_not_called()

    def test_main_identity_change_after_timed_out_read_stops_before_retry(self):
        api=self.api([]);metadata=api.metadata
        def scan(stage):
            api.metadata=lambda hwnd:{**metadata(hwnd),'pid':99}
            raise self.timeout_error()
        with self.assertRaises(WDMSError) as caught:
            nav.scan_page_state(api,self.root,self.log,scan,'step_3_after',25)
        self.assertEqual(caught.exception.code,'NAVIGATION_SESSION_CHANGED')
        self.assertEqual(self.clock[0],0);api.click_navigation_input.assert_not_called()

    def test_cancel_during_read_wait_prevents_retry_or_page_success(self):
        api=self.api([]);cancel=Path(self.temp.name)/'cancel.request'
        scan=mock.Mock(side_effect=self.timeout_error())
        def cancel_on_sleep(duration):
            self.clock[0]+=duration;cancel.touch()
        with mock.patch.object(nav.time,'sleep',side_effect=cancel_on_sleep):
            with self.assertRaises(WDMSError) as caught:
                nav.scan_page_state(api,self.root,self.log,scan,'step_3_after',25,cancel)
        self.assertEqual(caught.exception.code,'TASK_CANCELLED')
        self.assertEqual(scan.call_count,1);api.click_navigation_input.assert_not_called()

    def test_custom_caption_timeout_is_structured_and_may_be_reread(self):
        api=self.api([])
        api.user32.SendMessageTimeoutW=lambda *a:0
        item={'hwnd':90,'class_name':'WindowsForms10.Window.8.synthetic'}
        with mock.patch.object(ctypes,'get_last_error',return_value=1460):
            with self.assertRaises(NativeTextReadError) as caught:
                nav.navigation_caption(api,item,self.log)
        scan=mock.Mock(side_effect=[caught.exception,[]])
        self.assertEqual(nav.scan_page_state(api,self.root,self.log,scan,'step_3_after',25),[])
        self.assertEqual(scan.call_count,2)

    def test_success_after_deadline_is_rejected_instead_of_using_stale_events(self):
        api=self.api([])
        def slow_scan(stage):
            self.clock[0]=26
            return self.page()
        with self.assertRaises(WDMSError) as caught:
            nav.scan_page_state(api,self.root,self.log,slow_scan,'step_3_after',25)
        self.assertEqual(caught.exception.code,'NAVIGATION_SCAN_WAIT_TIMEOUT')

    def test_discarded_partial_scan_cannot_supply_missing_controls_to_next_scan(self):
        page=self.page();api=self.api([page]);attempts=[]
        incomplete=[e for e in page if e['hwnd']!=15]
        def scan(stage):
            path=self.log.with_name(f'page_scan_{len(attempts)+1:03d}.jsonl')
            attempts.append(path)
            view=page if len(attempts)==1 else incomplete
            for event in view:
                nav.probe.record(path,event['event'],**{k:v for k,v in event.items() if k!='event'})
            if len(attempts)==1:
                raise self.timeout_error()
            nav.probe.record(path,'native_scan_complete')
            return nav.probe.read_events(path)
        fresh=nav.scan_page_state(api,self.root,self.log,scan,'step_3_after',25)
        self.assertEqual(len(attempts),2)
        self.assertIsNotNone(nav.inventory_page_controls(nav.probe.read_events(attempts[0])))
        self.assertIsNone(nav.inventory_page_controls(fresh))
        self.assertFalse(nav.confirm_inventory_page(api,self.root,fresh,self.log,False))

    def test_unresponsive_or_disabled_main_during_wait_stops_instead_of_retrying(self):
        for disabled in (True,False):
            with self.subTest(disabled=disabled):
                api=self.api([])
                def scan(stage):
                    if disabled:
                        api.user32.IsWindowEnabled=lambda hwnd:False
                    else:
                        api.health.side_effect=WDMSError('WDMS_UNRESPONSIVE','合成窗口未响应')
                    raise self.timeout_error()
                raw=mock.Mock(side_effect=scan)
                with self.assertRaises(WDMSError) as caught:
                    nav.scan_page_state(api,self.root,self.log,raw,'step_3_after',self.clock[0]+25)
                self.assertEqual(caught.exception.code,'NAVIGATION_SESSION_CHANGED' if disabled else 'WDMS_UNRESPONSIVE')
                self.assertEqual(raw.call_count,1)

    def test_already_open_page_needs_no_click(self):
        page=self.page();api=self.api([page])
        nav.navigate(api,self.root,self.log,lambda stage:page)
        api.click_navigation_input.assert_not_called()

    def test_icon_home_menu_then_native_steps_still_require_ready_page(self):
        from desktop_demo import toolbar_icon
        views=[self.target('在库查询',2),self.target('在库商品检索',3),self.page()]
        api=self.api(views)
        def scan(stage):
            if stage=='step_1_after':return views[0]
            if stage=='step_2_after':return views[1]
            if stage=='step_3_after':return views[2]
            return []
        target={'strategy':'toolbar_icon','hwnd':20}
        fallback=mock.Mock(return_value=target)
        with mock.patch.object(toolbar_icon,'click_target') as click:
            nav.navigate(api,self.root,self.log,scan,fallback)
        fallback.assert_called_once_with()
        click.assert_called_once_with(api,self.root,target,self.log,None)
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[2,3])
        self.assertEqual(nav.probe.read_events(self.log)[-1]['event'],'inventory_page_ready')

    def expanded_inventory_menu(self):
        # Reproduce the allowed menu metadata from the 2026-10-13 Windows log:
        # an inventory panel and the item already visible at depth eight,
        # without a native caption for the query group. Handles/PID are synthetic.
        panel=self.target('在库管理',20)
        link=self.target('       在库商品检索',21)
        for event in panel:
            event.update(class_name='WindowsForms10.Window.8.app',rect=[0,100,196,703],depth=4)
        for event in link:
            event.update(rect=[22,155,216,180],depth=8)
        return panel+link

    def test_icon_click_can_reveal_inventory_link_without_group_caption(self):
        from desktop_demo import toolbar_icon
        menu=self.expanded_inventory_menu();page=self.page();api=self.api([menu,page])
        def scan(stage):
            if stage=='step_1_after':return menu
            if stage=='step_3_after':return page
            return []
        target={'strategy':'toolbar_icon','hwnd':30}
        with mock.patch.object(toolbar_icon,'click_target') as click:
            nav.navigate(api,self.root,self.log,scan,lambda:target)
        click.assert_called_once_with(api,self.root,target,self.log,None)
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[21])
        events=nav.probe.read_events(self.log)
        skipped=[e for e in events if e['event']=='navigation_step_skipped']
        self.assertEqual([(e['step'],e['name'],e['next_target']) for e in skipped],
                         [(2,'在库查询','在库商品检索')])
        self.assertEqual(events[-1]['event'],'inventory_page_ready')
        self.assertFalse(events[-1]['query_performed'])

    def test_already_expanded_menu_does_not_click_subsystem_or_group(self):
        menu=self.expanded_inventory_menu();page=self.page();api=self.api([menu,page])
        fallback=mock.Mock()
        nav.navigate(api,self.root,self.log,mock.Mock(side_effect=[menu,page]),fallback)
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[21])
        fallback.assert_not_called()
        self.assertEqual([e['step'] for e in nav.probe.read_events(self.log)
                          if e['event']=='navigation_step_skipped'],[1,2])

    def test_visible_group_is_not_toggled_when_its_link_is_already_visible(self):
        menu=self.target('在库查询',2)+self.target('在库商品检索',3)
        page=self.page();api=self.api([menu,page])
        nav.navigate(api,self.root,self.log,mock.Mock(side_effect=[menu,page]))
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[3])

    def test_group_becoming_visible_during_location_skips_home_icon(self):
        group=self.target('在库查询',2);link=self.target('在库商品检索',3);page=self.page()
        api=self.api([group,link,page]);fallback=mock.Mock()
        nav.navigate(api,self.root,self.log,mock.Mock(side_effect=[[],group,link,page]),fallback)
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[2,3])
        fallback.assert_not_called()

    def test_inventory_link_visibility_alone_is_not_page_success(self):
        menu=self.expanded_inventory_menu();api=self.api([menu])
        with self.assertRaises(WDMSError) as caught:
            nav.navigate(api,self.root,self.log,lambda stage:menu)
        self.assertEqual(caught.exception.code,'NAVIGATION_STEP_UNCONFIRMED')
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[21])
        self.assertFalse(any(e['event']=='inventory_page_ready' for e in nav.probe.read_events(self.log)))

    def test_ambiguous_visible_inventory_links_stop_before_click(self):
        menu=self.target('在库商品检索',3)+self.target('在库商品检索',4)
        api=self.api([menu])
        with self.assertRaises(WDMSError) as caught:
            nav.navigate(api,self.root,self.log,lambda stage:menu)
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_AMBIGUOUS')
        api.click_navigation_input.assert_not_called()

    def test_hidden_inventory_link_does_not_skip_group_click(self):
        group=self.target('在库查询',2);hidden=self.target('在库商品检索',3)
        for event in hidden:event['visible']=False
        link=self.target('在库商品检索',3);page=self.page();api=self.api([group,hidden,link,page])
        nav.navigate(api,self.root,self.log,mock.Mock(side_effect=[group+hidden,link,page]))
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[2,3])

    def test_already_open_page_with_changed_process_is_not_confirmed(self):
        page=self.page();api=self.api([page]);metadata=api.metadata
        api.metadata=lambda hwnd:{**metadata(hwnd),'pid':99}
        with self.assertRaises(WDMSError) as caught:
            nav.navigate(api,self.root,self.log,lambda stage:page)
        self.assertEqual(caught.exception.code,'NAVIGATION_SESSION_CHANGED')
        api.click_navigation_input.assert_not_called()
        self.assertFalse(self.log.exists())

    def sidebar(self,expanded=False):
        result=[]
        for hwnd,parent,rect,text in (
            (20,100,[0,100,196,703],'在库管理'),
            (22,20,[1,101,195,702],''),
            (23,22,[9,109,187,193 if expanded else 142],''),
            (24,23,[14,147,230,747],''),
            (25,24,[22,155,216,180],'       在库商品检索')):
            node={'event':'native_control','hwnd':hwnd,'pid':7,
                  'class_name':'WindowsForms10.STATIC.app' if hwnd==25 else 'WindowsForms10.Window.8.app',
                  'visible':True,'rect':rect,'parent_hwnd':parent}
            result.append(node)
            if text:result.append({**node,'event':'native_text','text':text})
        return result

    def staged_scan(self,api,views):
        iterator=iter(views)
        def scan(stage):
            view=next(iterator)
            api.set_view(view)
            return view
        return scan

    def test_style_visible_link_clipped_by_group_requires_expansion(self):
        closed=self.sidebar();opened=self.sidebar(True);page=self.page()
        api=self.api([closed,opened,page])
        nav.navigate(api,self.root,self.log,self.staged_scan(api,[closed,opened,page]))
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[23,25])
        group=api.click_navigation_input.call_args_list[0].args[1]
        self.assertEqual(group['header_rect'],[9,109,187,142])
        self.assertEqual(group['source_geometry']['clipped_by'],23)
        self.assertTrue(group['source_link']['visible'])
        events=nav.probe.read_events(self.log)
        self.assertFalse(any(e['event']=='navigation_step_skipped' and e['step']==2 for e in events))
        self.assertEqual(events[-1]['event'],'inventory_page_ready')

    def test_home_icon_then_collapsed_group_then_inventory_page(self):
        from desktop_demo import toolbar_icon
        closed=self.sidebar();opened=self.sidebar(True);page=self.page()
        api=self.api([closed,opened,page])
        def scan(stage):
            view={'step_1_after':closed,'step_2_after':opened,'step_3_after':page}.get(stage,[])
            api.set_view(view)
            return view
        with mock.patch.object(toolbar_icon,'click_target') as click:
            nav.navigate(api,self.root,self.log,scan,lambda:{'hwnd':30,'strategy':'toolbar_icon'})
        click.assert_called_once()
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[23,25])

    def test_truly_expanded_group_is_not_collapsed_again(self):
        opened=self.sidebar(True);page=self.page();api=self.api([opened,page])
        nav.navigate(api,self.root,self.log,self.staged_scan(api,[opened,page]))
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[25])
        self.assertEqual(api.click_navigation_input.call_args.args[1]['geometry']['visible_rect'],[22,155,187,180])

    def test_failed_group_expansion_never_clicks_clipped_inventory_link(self):
        closed=self.sidebar();api=self.api([closed])
        with self.assertRaises(WDMSError) as caught:
            nav.navigate(api,self.root,self.log,lambda stage:closed)
        self.assertEqual(caught.exception.code,'NAVIGATION_STEP_UNCONFIRMED')
        self.assertEqual([c.args[1]['hwnd'] for c in api.click_navigation_input.call_args_list],[23])
        self.assertFalse(any(e['event']=='inventory_page_ready' for e in nav.probe.read_events(self.log)))

    def test_sidebar_group_with_changed_subsystem_caption_stops(self):
        closed=self.sidebar();api=self.api([closed]);caption=api.user32.SendMessageTimeoutW
        def changed_caption(hwnd,*args):
            if hwnd==20:
                buffer=ctypes.create_unicode_buffer('企业管理')
                ctypes.memmove(args[2],buffer,ctypes.sizeof(buffer));args[-1]._obj.value=4
                return True
            return caption(hwnd,*args)
        api.user32.SendMessageTimeoutW=changed_caption
        with self.assertRaises(WDMSError) as caught:
            nav.navigate(api,self.root,self.log,lambda stage:closed)
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_CHANGED')
        api.click_navigation_input.assert_not_called()

    def test_group_expanded_before_press_is_not_toggled(self):
        closed=self.sidebar();opened=self.sidebar(True);api=self.api([closed,opened])
        api.set_view(closed)
        target=nav.sidebar_group_target(api,self.root,closed,self.log)
        api.set_view(opened)
        self.assertFalse(nav.validate_before_navigation_press(api,self.root,target,'在库查询',self.log))

    def test_query_page_caption_is_not_an_additional_menu_link(self):
        opened=self.sidebar(True)
        title=self.target('在库商品检索',40)
        for event in title:event['class_name']='WindowsForms10.Window.8.app'
        self.assertEqual(nav.navigation_target(opened+title,'在库商品检索',7)['hwnd'],25)

    def test_visible_page_with_disabled_query_button_needs_no_query_or_click(self):
        page=self.page();api=self.api([page])
        api.user32.IsWindowEnabled=lambda hwnd:hwnd!=13
        nav.navigate(api,self.root,self.log,lambda stage:page)
        self.assertEqual(nav.probe.read_events(self.log)[-1]['event'],'inventory_page_ready')
        api.click_navigation_input.assert_not_called()

    def test_duplicate_menu_targets_stop_before_click(self):
        view=self.target('在库管理',1)+self.target('在库管理',2);api=self.api([view])
        with self.assertRaises(WDMSError) as caught:nav.navigate(api,self.root,self.log,lambda stage:view)
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_AMBIGUOUS')
        api.click_navigation_input.assert_not_called()

    def test_no_transition_does_not_repeat_menu_click(self):
        view=self.target('在库管理',1);api=self.api([view])
        with self.assertRaises(WDMSError) as caught:nav.navigate(api,self.root,self.log,lambda stage:view)
        self.assertEqual(caught.exception.code,'NAVIGATION_STEP_UNCONFIRMED')
        api.click_navigation_input.assert_called_once()

    def test_stale_live_text_stops_before_click(self):
        view=self.target('在库管理',1);api=self.api([view]);api.read_label_text=lambda data,log:'其他功能'
        with self.assertRaises(WDMSError) as caught:nav.navigate(api,self.root,self.log,lambda stage:view)
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_CHANGED')
        api.click_navigation_input.assert_not_called()

    def test_missing_menu_is_not_guessed(self):
        api=self.api([])
        with self.assertRaises(WDMSError) as caught:nav.navigate(api,self.root,self.log,lambda stage:[])
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_NOT_FOUND')
        api.click_navigation_input.assert_not_called()

    def test_missing_home_menu_collects_once_without_claiming_navigation(self):
        api=self.api([]);diagnostic=mock.Mock()
        with self.assertRaises(WDMSError) as caught:
            nav.navigate(api,self.root,self.log,lambda stage:[],diagnostic)
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_NOT_FOUND')
        diagnostic.assert_called_once_with()
        api.click_navigation_input.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_ambiguous_menu_does_not_start_diagnostic(self):
        view=self.target('在库管理',1)+self.target('在库管理',2)
        diagnostic=mock.Mock()
        with self.assertRaises(WDMSError):
            nav.navigate(self.api([view]),self.root,self.log,lambda stage:view,diagnostic)
        diagnostic.assert_not_called()

    def test_diagnostic_logs_partial_result_and_propagates_cancel_path(self):
        from scripts import diagnose_wdms_menu
        cancel=Path(self.temp.name)/'cancel'
        with mock.patch.object(diagnose_wdms_menu,'main',return_value=2) as run:
            nav.collect_menu_diagnostic(self.log,cancel)
        run.assert_called_once_with(['--out',str(self.log.parent/f'{self.log.stem}_menu'),
                                     '--cancel-file',str(cancel)])
        event=nav.probe.read_events(self.log)[-1]
        self.assertEqual(event['exit_code'],2)
        self.assertFalse(event['navigation_completed'])

    def test_diagnostic_exception_is_recorded_without_escaping(self):
        from scripts import diagnose_wdms_menu
        with mock.patch.object(diagnose_wdms_menu,'main',side_effect=OSError('test')):
            nav.collect_menu_diagnostic(self.log)
        self.assertEqual(nav.probe.read_events(self.log)[-1]['event'],'navigation_menu_diagnostic_error')

    def test_parent_passes_cancel_path_to_child(self):
        cancel=Path(self.temp.name)/'cancel'
        with mock.patch.object(nav.probe,'supervise',return_value=2) as supervise:
            nav.main(['--out',self.temp.name,'--cancel-file',str(cancel)])
        command=supervise.call_args.args[0]
        self.assertEqual(command[-2:],['--cancel-file',str(cancel.resolve())])

    def test_custom_panel_navigation_caption_is_mapped(self):
        view=self.target('在库管理',1)
        for e in view:e['class_name']='WindowsForms10.Window.8.app'
        self.assertEqual(nav.navigation_target(view,'在库管理',7)['hwnd'],1)

    def test_custom_caption_read_timeout_stops(self):
        api=SimpleNamespace(user32=SimpleNamespace(SendMessageTimeoutW=lambda *a:0))
        with self.assertRaises(WDMSError):nav.navigation_caption(api,{'hwnd':1,'class_name':'WindowsForms10.Window.8.app'},self.log)

    def test_page_requires_both_quantity_labels(self):
        view=[e for e in self.page() if e['hwnd']!=15]
        self.assertIsNone(nav.inventory_page_controls(view))

    def test_frozen_navigation_routes_without_gui_or_source_path(self):
        with mock.patch.object(nav.sys,'frozen',True,create=True),mock.patch.object(nav.probe,'supervise',return_value=0) as supervise:
            self.assertEqual(nav.main(['--out',self.temp.name]),0)
        command=supervise.call_args.args[0]
        self.assertEqual(command[:3],[nav.sys.executable,'--native-navigate','--_child'])

    def test_child_entry_records_scan_and_verifies_ready_page(self):
        page=self.page();api=self.api([page])
        def scan(api,root,path,**kwargs):
            for event in page:
                nav.probe.record(path,event['event'],**{k:v for k,v in event.items() if k!='event'})
        with mock.patch.object(nav,'load_settings',return_value=SimpleNamespace(wdms_exe='App.exe')),mock.patch.object(nav,'validate_executable',return_value='App.exe'),mock.patch.object(nav.probe,'inspect_environment',return_value={}),mock.patch.object(nav.probe,'require_usable_environment'),mock.patch.object(nav,'NavigationNativeWindows',return_value=api),mock.patch.object(nav,'find_stage_window',return_value=self.root),mock.patch.object(nav,'pid_matches',return_value=True),mock.patch.object(nav.probe,'check_native_controls',side_effect=scan):
            code=nav.child(SimpleNamespace(log=str(self.log)))
        self.assertEqual(code,0)
        events=nav.probe.read_events(self.log)
        self.assertEqual(events[0]['event'],'navigation_scan')
        self.assertTrue(Path(events[0]['scan_file']).is_file())
        self.assertEqual(events[-1]['event'],'automatic_navigation_complete')
        api.click_navigation_input.assert_not_called()


if __name__=='__main__':unittest.main()
