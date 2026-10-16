import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_demo import navigation_native as native
from errors import WDMSError


class NavigationNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/'navigation.jsonl'
        self.root={'hwnd':100,'pid':7}

    def api(self,expanded=True,hit=25):
        rows={
            100:([0,0,800,600],0,'Window.8.'),
            20:([0,80,200,550],100,'Window.8.'),
            23:([10,100,190,182 if expanded else 132],20,'Window.8.'),
            24:([15,140,225,540],23,'Window.8.'),
            25:([25,150,205,174],24,'STATIC.'),
        }
        nodes={hwnd:{'hwnd':hwnd,'pid':7,'class_name':'WindowsForms10.'+kind+'app',
                     'rect':rect,'visible':True,'parent':parent} for hwnd,(rect,parent,kind) in rows.items()}
        cursor=[0,0]
        def get_client(hwnd,pointer):
            rect=nodes[hwnd].get('client_rect',nodes[hwnd]['rect'])
            pointer._obj.left=0;pointer._obj.top=0
            pointer._obj.right=rect[2]-rect[0];pointer._obj.bottom=rect[3]-rect[1]
            return True
        def to_screen(hwnd,pointer):
            rect=nodes[hwnd].get('client_rect',nodes[hwnd]['rect'])
            pointer._obj.x+=rect[0];pointer._obj.y+=rect[1]
            return True
        def to_client(hwnd,pointer):
            rect=nodes[hwnd].get('client_rect',nodes[hwnd]['rect'])
            pointer._obj.x-=rect[0];pointer._obj.y-=rect[1]
            return True
        def set_cursor(x,y):cursor[:]=[x,y];return True
        def get_cursor(pointer):pointer._obj.x,pointer._obj.y=cursor;return True
        api=object.__new__(native.NavigationNativeWindows)
        api.metadata=lambda hwnd:nodes[hwnd]
        api.health=mock.Mock()
        api.nodes=nodes
        api.user32=SimpleNamespace(
            GetParent=lambda hwnd:nodes[hwnd]['parent'],
            GetClientRect=get_client,ClientToScreen=to_screen,ScreenToClient=to_client,
            IsWindowEnabled=lambda hwnd:nodes[hwnd].get('enabled',True),
            SetForegroundWindow=lambda hwnd:True,GetForegroundWindow=lambda:100,
            WindowFromPoint=lambda point:hit,
            ChildWindowFromPointEx=lambda hwnd,point,flags:25 if hwnd==24 else 0,
            GetAsyncKeyState=lambda key:0,SetCursorPos=set_cursor,GetCursorPos=get_cursor,
            SendInput=mock.Mock(return_value=2),SendMessageTimeoutW=mock.Mock())
        return api

    def target(self,api,hwnd=25,header=None):
        result={**api.metadata(hwnd)}
        result['geometry']=native.inspect_geometry(api,self.root,result)
        if header is not None:result['header_rect']=header
        return result

    def click(self,api,target,before_press=lambda:True):
        with mock.patch.object(native.time,'sleep'):
            return api.click_navigation_input(self.root,target,self.log,before_press)

    def test_style_visible_descendant_outside_collapsed_group_is_not_rendered(self):
        api=self.api(expanded=False)
        geometry=native.inspect_geometry(api,self.root,api.metadata(25))
        self.assertTrue(api.metadata(25)['visible'])
        self.assertIsNone(geometry['visible_rect'])
        self.assertEqual(geometry['clipped_by'],23)
        with self.assertRaises(WDMSError):self.click(api,self.target(api))
        api.user32.SendInput.assert_not_called()
        api.user32.SendMessageTimeoutW.assert_not_called()

    def test_expanded_link_uses_intersection_of_all_client_areas(self):
        api=self.api()
        geometry=native.inspect_geometry(api,self.root,api.metadata(25))
        self.assertEqual(geometry['visible_rect'],[25,150,190,174])
        self.assertEqual([node['hwnd'] for node in geometry['chain']],[25,24,23,20,100])

    def test_disabled_ancestor_prevents_rendered_target(self):
        api=self.api();api.nodes[20]['enabled']=False
        geometry=native.inspect_geometry(api,self.root,api.metadata(25))
        self.assertIsNone(geometry['visible_rect'])
        self.assertEqual(geometry['unavailable'],20)
        self.assertIsNone(native.inspect_geometry(api,self.root,api.metadata(25),
                                                  require_enabled=False)['visible_rect'])

    def test_hidden_ancestor_prevents_input_even_with_visible_leaf(self):
        api=self.api();api.nodes[20]['visible']=False
        with self.assertRaises(WDMSError):self.click(api,self.target(api))
        api.user32.SendInput.assert_not_called()

    def test_parent_chain_must_reach_configured_root(self):
        api=self.api();api.nodes[20]['parent']=0
        with self.assertRaises(WDMSError) as caught:
            native.inspect_geometry(api,self.root,api.metadata(25))
        self.assertEqual(caught.exception.code,'NAVIGATION_ANCESTRY_INVALID')

    def test_parent_cycle_is_rejected(self):
        api=self.api();api.nodes[20]['parent']=24
        with self.assertRaises(WDMSError) as caught:
            native.inspect_geometry(api,self.root,api.metadata(25))
        self.assertEqual(caught.exception.code,'NAVIGATION_ANCESTRY_INVALID')

    def test_foreign_parent_process_is_rejected(self):
        api=self.api();api.nodes[20]['pid']=99
        with self.assertRaises(WDMSError) as caught:
            native.inspect_geometry(api,self.root,api.metadata(25))
        self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_CHANGED')

    def test_group_header_click_sends_one_physical_pair(self):
        api=self.api(expanded=False,hit=23)
        self.assertTrue(self.click(api,self.target(api,23,[10,100,190,132])))
        api.user32.SendInput.assert_called_once()
        count,inputs,size=api.user32.SendInput.call_args.args
        self.assertEqual(count,2)
        self.assertEqual([item.mi.dwFlags for item in inputs],[2,4])
        event=[e for e in native.probe.read_events(self.log) if e['event']=='before_navigation_mouse_input'][0]
        self.assertEqual((event['x'],event['y']),(100,116))
        api.user32.SendMessageTimeoutW.assert_not_called()

    def test_transparent_static_parent_hit_uses_physical_input(self):
        api=self.api(hit=24)
        self.assertTrue(self.click(api,self.target(api)))
        api.user32.SendInput.assert_called_once()
        api.user32.SendMessageTimeoutW.assert_not_called()

    def test_sibling_or_external_window_hit_is_rejected(self):
        for hit in (99,999):
            with self.subTest(hit=hit):
                api=self.api(hit=hit)
                with self.assertRaises(WDMSError) as caught:self.click(api,self.target(api))
                self.assertEqual(caught.exception.code,'NAVIGATION_TARGET_OBSCURED')
                api.user32.SendInput.assert_not_called()

    def test_transparent_parent_hit_with_other_child_is_rejected(self):
        api=self.api(hit=24);api.user32.ChildWindowFromPointEx=lambda *args:99
        with self.assertRaises(WDMSError):self.click(api,self.target(api))
        api.user32.SendInput.assert_not_called()

    def test_partial_input_releases_only_and_never_repeats_down(self):
        api=self.api();api.user32.SendInput.side_effect=[1,1]
        with self.assertRaises(WDMSError) as caught:self.click(api,self.target(api))
        self.assertEqual(caught.exception.code,'NAVIGATION_INPUT_FAILED')
        self.assertEqual([call.args[0] for call in api.user32.SendInput.call_args_list],[2,1])
        self.assertEqual(api.user32.SendInput.call_args_list[1].args[1]._obj.mi.dwFlags,4)

    def test_ancestor_resize_during_hover_stops_before_input(self):
        api=self.api();target=self.target(api)
        def resize(seconds):api.nodes[23]['rect']=[10,100,190,132]
        with mock.patch.object(native.time,'sleep',side_effect=resize):
            with self.assertRaises(WDMSError):
                api.click_navigation_input(self.root,target,self.log,lambda:True)
        api.user32.SendInput.assert_not_called()

    def test_already_expanded_before_press_cancels_without_input(self):
        api=self.api(expanded=False,hit=23)
        self.assertFalse(self.click(api,self.target(api,23,[10,100,190,132]),lambda:False))
        api.user32.SendInput.assert_not_called()

    def test_input_busy_stops_before_mouse_move_or_input(self):
        api=self.api();api.user32.GetAsyncKeyState=lambda key:0x8000 if key==16 else 0
        with self.assertRaises(WDMSError) as caught:self.click(api,self.target(api))
        self.assertEqual(caught.exception.code,'NAVIGATION_INPUT_BUSY')
        api.user32.SendInput.assert_not_called()

    def test_client_rect_is_converted_from_client_to_screen_coordinates(self):
        api=self.api();api.nodes[25]['client_rect']=[27,152,203,172]
        self.assertEqual(api.client_screen_rect(25),[27,152,203,172])


if __name__=='__main__':unittest.main()
