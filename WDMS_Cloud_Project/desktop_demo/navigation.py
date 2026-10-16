"""Bounded native navigation test; no credentials, owner changes or queries."""
from __future__ import annotations
import argparse
import ctypes
import json
import math
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from desktop_demo.login import normalized, pid_matches
from desktop_demo.navigation_native import NavigationNativeWindows, inspect_geometry, intersection, same_window
from desktop_demo.settings import load_settings, settings_path, validate_executable
from scripts.check_wdms_startup import find_stage_window
from scripts import check_wdms_window as probe
from errors import NativeTextReadError, WDMSError
from adapters.native_windows import message_policy


NAVIGATION_STEPS = ('在库管理', '在库查询', '在库商品检索')
NAVIGATION_REVISION = 'native-navigation-v9-page-wait'


def navigation_target(events, name, pid):
    nodes={e['hwnd']:e for e in events if e['event']=='native_control'}
    matches=[]
    for e in events:
        if e['event']!='native_text' or normalized(e.get('text',''))!=name:
            continue
        node=nodes.get(e['hwnd'])
        if node and node.get('visible') and node['pid']==pid and node['class_name'].startswith(('WindowsForms10.STATIC.','WindowsForms10.BUTTON.','WindowsForms10.Window.8.')):
            if name=='在库商品检索' and node['class_name'].startswith('WindowsForms10.Window.8.'):
                continue  # Page captions are not sidebar menu links.
            matches.append({**node,'text':e['text']})
    if len(matches)>1:
        raise WDMSError('NAVIGATION_TARGET_AMBIGUOUS',f'{name}控件不唯一，未继续点击。')
    return matches[0] if matches else None


def navigation_caption(api,item,log):
    if not item['class_name'].startswith('WindowsForms10.Window.8.'):
        return api.read_label_text(item,log)
    buffer=ctypes.create_unicode_buffer(512)
    result=ctypes.c_size_t()
    ctypes.set_last_error(0)
    ok=api.user32.SendMessageTimeoutW(item['hwnd'],0x000D,len(buffer),ctypes.cast(buffer,ctypes.c_void_p).value,0x23,message_policy(api).text_read_ms,ctypes.byref(result))
    if not ok:
        code=ctypes.get_last_error()
        raise NativeTextReadError('NAVIGATION_CAPTION_UNAVAILABLE',
            f'自定义菜单文字读取未成功，WinError={code}；停止本次扫描。',
            winerror=code,hwnd=item['hwnd'],class_name=item['class_name'])
    if result.value>=len(buffer)-1:
        raise WDMSError('NAVIGATION_CAPTION_UNAVAILABLE','自定义菜单文字无法完整读取，停止导航。')
    return buffer.value


def supplement_navigation_captions(api,root,events,log):
    for node in list(events):
        if node['event']!='native_control' or not node.get('visible') or not node['class_name'].startswith('WindowsForms10.Window.8.'):
            continue
        live=api.metadata(node['hwnd'])
        if live['pid']!=root['pid'] or live['class_name']!=node['class_name'] or not live['visible']:
            raise WDMSError('NAVIGATION_TARGET_CHANGED','自定义菜单窗口身份变化。')
        text=navigation_caption(api,live,log)
        # Retain only navigation names, never arbitrary panel/grid text.
        if normalized(text) in {'在库管理','在库查询','在库商品检索'}:
            event={**live,'event':'native_text','text':text}
            events.append(event)
            probe.record(log,'native_text',**{k:v for k,v in event.items() if k!='event'})
    return events


def inventory_page_controls(events):
    # A newly opened page may have no numerical results yet. Confirm the
    # navigation and input controls without requiring a first query.
    try:
        owner=probe.owner_combo_from_events(events)
    except WDMSError:
        return None
    texts=[e for e in events if e['event']=='native_text' and e.get('visible',True)]
    if not any(normalized(e['text'])=='在库商品检索' for e in texts):return None
    controls={'owner':owner}
    for key,name,kind in (('search','查询','BUTTON'),('whole_label','整件数','STATIC'),('loose_label','散件数','STATIC')):
        matches=[e for e in texts if e['class_name'].startswith(f'WindowsForms10.{kind}.') and normalized(e['text'])==name]
        if len(matches)!=1:return None
        controls[key]=matches[0]
    return controls


def confirm_inventory_page(api, root, events, log, already_open):
    controls = inventory_page_controls(events)
    if not controls:
        return False
    for item in controls.values():
        live = api.metadata(item['hwnd'])
        if (live['pid'] != root['pid'] or live['class_name'] != item['class_name']
                or not live['visible']):
            raise WDMSError('NAVIGATION_PAGE_CHANGED', '检索页面控件身份变化，未确认导航完成。')
        if inspect_geometry(api,root,{**item,**live},require_enabled=False)['visible_rect'] is None:
            return False
    probe.record(log, 'inventory_page_ready', already_open=already_open,
                 query_performed=False, owner_hwnd=controls['owner']['hwnd'],
                 search_hwnd=controls['search']['hwnd'])
    return True


def sidebar_group_target(api, root, events, log):
    link=navigation_target(events,'在库商品检索',root['pid'])
    if not link:return None
    geometry=inspect_geometry(api,root,link)
    chain=geometry['chain']
    # The known sidebar has a group hosting a list panel hosting the link.
    # Derive that group only when its client area is what clips the link.
    if (geometry['visible_rect'] is not None or geometry['unavailable'] is not None
            or len(chain)<4 or geometry['clipped_by']!=chain[2]['hwnd']
            or not all(node['class_name'].startswith('WindowsForms10.Window.8.')
                       for node in chain[1:3])):
        return None
    body,group=chain[1:3]
    possible_sidebar={node['hwnd']:node for node in chain[3:-1]}
    captions={event['hwnd'] for event in events if event['event']=='native_text'
              and normalized(event.get('text',''))=='在库管理'
              and event['hwnd'] in possible_sidebar}
    if len(captions)!=1:
        return None
    sidebar=possible_sidebar[next(iter(captions))]
    if normalized(navigation_caption(api,sidebar,log))!='在库管理':
        raise WDMSError('NAVIGATION_TARGET_CHANGED','侧栏所属子系统变化，未展开查询分组。')
    group_geometry=inspect_geometry(api,root,group)
    if group_geometry['visible_rect'] is None:return None
    left,top,right,bottom=group['client_rect']
    header=intersection(group_geometry['visible_rect'],
                        [left,top,right,min(bottom,body['client_rect'][1])])
    # A header is the native band above this link's list, not an arbitrary
    # container centre. Relative size checks scale with the actual label.
    label_height=link['rect'][3]-link['rect'][1]
    if (header is None or label_height<=0 or header[3]-header[1]>3*label_height
            or intersection([left,link['rect'][1],right,link['rect'][3]],link['rect']) is None):
        return None
    target={**group,'text':'在库查询','strategy':'sidebar_group',
            'geometry':group_geometry,'header_rect':header,
            'source_link':link,'source_geometry':geometry,'sidebar':sidebar}
    probe.record(log,'sidebar_group_target',name='在库查询',hwnd=group['hwnd'],
                 header_rect=header,source_link_hwnd=link['hwnd'],
                 source_link_rect=link['rect'],body_rect=body['client_rect'],
                 clipped_by=geometry['clipped_by'],sidebar_hwnd=sidebar['hwnd'])
    return target


def navigation_candidate(api, root, events, name, log):
    target=navigation_target(events,name,root['pid'])
    if (target and name=='在库管理'
            and target['class_name'].startswith('WindowsForms10.Window.8.')):
        return None  # A sidebar caption is subsystem state, not its toolbar button.
    if target:
        geometry=inspect_geometry(api,root,target)
        if geometry['visible_rect'] is not None:
            return {**target,'geometry':geometry}
        probe.record(log,'navigation_target_clipped',name=name,hwnd=target['hwnd'],
                     rect=target['rect'],clipped_by=geometry['clipped_by'],
                     unavailable=geometry['unavailable'])
    if name=='在库查询':
        return sidebar_group_target(api,root,events,log)
    return None


def validate_before_navigation_press(api, root, target, name, log):
    if target.get('strategy')=='sidebar_group':
        link=target['source_link']
        geometry=inspect_geometry(api,root,link)
        if normalized(navigation_caption(api,link,log))!='在库商品检索':
            raise WDMSError('NAVIGATION_TARGET_CHANGED','查询分组的检索菜单文字变化，未展开。')
        if geometry['visible_rect'] is not None:
            return False  # Do not collapse a group expanded while activating.
        if geometry!=target['source_geometry']:
            raise WDMSError('NAVIGATION_TARGET_CHANGED','查询分组与检索项的父窗口关系变化，未展开。')
        sidebar=target['sidebar']
        live=api.metadata(sidebar['hwnd'])
        if (not same_window(sidebar,live)
                or normalized(navigation_caption(api,live,log))!='在库管理'):
            raise WDMSError('NAVIGATION_TARGET_CHANGED','在库侧栏身份或标题变化，未展开查询分组。')
    elif normalized(navigation_caption(api,target,log))!=name:
        raise WDMSError('NAVIGATION_TARGET_CHANGED','导航文字变化，未点击。')
    return True


def later_navigation_target(api, root, events, step, log):
    # Only the actually rendered part of a later control permits skipping.
    for name in reversed(NAVIGATION_STEPS[step:]):
        target = navigation_candidate(api, root, events, name, log)
        if target:
            return target
    return None


def collect_menu_diagnostic(log, cancel_file=None):
    from scripts import diagnose_wdms_menu
    folder=log.parent/f'{log.stem}_menu'
    probe.record(log,'navigation_menu_diagnostic_start',folder=str(folder),read_only=True)
    argv=['--out',str(folder)]
    if cancel_file is not None:
        argv.extend(['--cancel-file',str(cancel_file)])
    try:
        code=diagnose_wdms_menu.main(argv)
        probe.record(log,'navigation_menu_diagnostic_end',exit_code=code,folder=str(folder),
                     navigation_completed=False)
    except Exception as error:
        probe.record(log,'navigation_menu_diagnostic_error',**probe.error_info(error))
    return folder


def resolve_home_menu(root, log, cancel_file=None):
    from desktop_demo import toolbar_icon
    folder=collect_menu_diagnostic(log,cancel_file)
    requests=list(folder.glob('menu_*/request.json'))
    if len(requests)!=1:
        raise WDMSError('ICON_DIAGNOSTIC_MISSING','未取得唯一的工具栏诊断，未识别或点击。')
    request_path=requests[0]
    events=probe.read_events(request_path.with_name('msaa.jsonl'))
    request=json.loads(request_path.read_text(encoding='utf-8'))
    toolbar=toolbar_icon.choose_toolbar(request,events,root)
    return toolbar_icon.capture_target(root,toolbar,log,cancel_file)


def check_scan_session(api, root, cancel_file):
    if cancel_file is not None and Path(cancel_file).exists():
        raise WDMSError('TASK_CANCELLED', '任务已取消，未继续读取页面。')
    live = api.metadata(root['hwnd'])
    if (live['pid'] != root['pid'] or live['class_name'] != root['class_name']
            or not live['visible'] or live.get('minimized', False)
            or not api.user32.IsWindowEnabled(root['hwnd'])):
        raise WDMSError('NAVIGATION_SESSION_CHANGED', '页面读取期间WDMS主窗口身份或状态变化，停止本轮。')


def scan_page_state(api, root, log, scan, stage, deadline, cancel_file=None):
    """Retry only timed-out reads, under the caller's existing wait deadline.

    A failed scan returns no events. Each attempt must create a fresh scan log,
    so partial controls can never be merged into a later page confirmation.
    Mouse input, owner selection and business queries are never retried here.
    """
    attempts = 0
    last_error = None
    while True:
        check_scan_session(api, root, cancel_file)
        if time.monotonic() >= deadline:
            probe.record(log, 'page_scan_wait_exhausted', stage=stage, attempts=attempts,
                         read_only=True, input_repeated=False)
            if last_error is not None:
                raise last_error
            raise WDMSError('NAVIGATION_SCAN_WAIT_TIMEOUT', '等待页面完整读取超时，未确认导航完成。')
        api.health(root['hwnd'], log, 'before_page_scan')
        attempts += 1
        try:
            events = scan(stage)
        except NativeTextReadError as error:
            if (error.winerror != 1460 or error.code not in
                    {'NATIVE_TEXT_READ_FAILED', 'NAVIGATION_CAPTION_UNAVAILABLE'}):
                raise
            last_error = error
            check_scan_session(api, root, cancel_file)
            probe.record(log, 'page_scan_deferred', stage=stage, attempt=attempts,
                         read_only=True, input_repeated=False, **probe.error_info(error))
            time.sleep(min(0.4, max(0, deadline-time.monotonic())))
            continue
        check_scan_session(api, root, cancel_file)
        if time.monotonic() >= deadline:
            raise WDMSError('NAVIGATION_SCAN_WAIT_TIMEOUT', '页面读取超过本阶段等待期限，未使用过期结果。')
        if last_error is not None:
            probe.record(log, 'page_scan_recovered', stage=stage, attempts=attempts,
                         read_only=True, input_repeated=False)
        return events


def navigate(api,root,log,scan,on_missing_home_menu=None,cancel_file=None):
    def read(stage, deadline):
        return scan_page_state(api, root, log, scan, stage, deadline, cancel_file)
    events=read('initial', time.monotonic()+25)
    if confirm_inventory_page(api,root,events,log,already_open=True):
        return
    for index,name in enumerate(NAVIGATION_STEPS,1):
        deadline=time.monotonic()+(2 if index==1 else 20)
        target=None
        later=None
        while time.monotonic()<deadline:
            later=later_navigation_target(api,root,events,index,log)
            if later:break
            target=navigation_candidate(api,root,events,name,log)
            if target:break
            time.sleep(min(0.4, max(0, deadline-time.monotonic())))
            if time.monotonic() >= deadline:
                break
            # A metadata scan already had a 5s budget before this change.
            # Do not abort a complete last scan just because the short toolbar
            # lookup interval ends; its result may permit the icon fallback.
            events=read(f'step_{index}_locate', max(deadline, time.monotonic()+5))
            if confirm_inventory_page(api,root,events,log,already_open=True):
                return
        if later:
            probe.record(log,'navigation_step_skipped',step=index,name=name,
                         reason='later_target_visible',next_target=normalized(later['text']),
                         next_hwnd=later['hwnd'])
            continue
        if target is None:
            if index==1 and on_missing_home_menu is not None:
                candidate=on_missing_home_menu()
                if isinstance(candidate,dict):target=candidate
            if target is None:
                raise WDMSError('NAVIGATION_TARGET_NOT_FOUND',f'未找到可唯一定位的“{name}”控件。未猜测坐标，请上传本轮导航日志及 scan 日志。')
        if target.get('strategy')=='toolbar_icon':
            from desktop_demo import toolbar_icon
            toolbar_icon.click_target(api,root,target,log,cancel_file)
        else:
            live=api.metadata(target['hwnd'])
            if live['pid']!=root['pid'] or live['class_name']!=target['class_name'] or not live['visible'] or live['rect']!=target['rect'] or not api.user32.IsWindowEnabled(target['hwnd']):
                raise WDMSError('NAVIGATION_TARGET_CHANGED','导航控件身份、位置或可用状态变化，未点击。')
            api.health(root['hwnd'],log,f'before_navigation_{index}')
            probe.record(log,'before_navigation_click',step=index,name=name,hwnd=target['hwnd'])
            permitted=validate_before_navigation_press(api,root,target,name,log)
            clicked=(api.click_navigation_input(root,target,log,
                before_press=lambda:validate_before_navigation_press(api,root,target,name,log))
                if permitted else False)
            if clicked is False:
                probe.record(log,'navigation_step_skipped',step=index,name=name,
                             reason='group_expanded_before_press',next_target='在库商品检索')
            else:
                probe.record(log,'navigation_click_sent',step=index,name=name,automatic_retries=False)
        if target.get('strategy')=='toolbar_icon':
            probe.record(log,'navigation_click_sent',step=index,name=name,automatic_retries=False)
        # Wait until the next target is available, or the actual query page is
        # mapped. Never repeat a click merely because rendering is delayed.
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            time.sleep(min(0.4, max(0, deadline-time.monotonic())))
            if time.monotonic() >= deadline:
                raise WDMSError('NAVIGATION_STEP_UNCONFIRMED',f'点击“{name}”后未确认下一步，停止且不重复点击。')
            events=read(f'step_{index}_after', deadline)
            if confirm_inventory_page(api,root,events,log,already_open=False):
                return
            if index<3:
                later=later_navigation_target(api,root,events,index,log)
                if later:
                    probe.record(log,'navigation_step_confirmed',step=index,
                                 next_target=normalized(later['text']))
                    break
        else:
            raise WDMSError('NAVIGATION_STEP_UNCONFIRMED',f'点击“{name}”后未确认下一步，停止且不重复点击。')


def child(args):
    log=Path(args.log)
    try:
        config=load_settings()
        if config is None:raise WDMSError('SETTINGS_MISSING','请先保存本机配置。')
        exe=validate_executable(config.wdms_exe)
        probe.require_usable_environment(probe.inspect_environment())
        api=NavigationNativeWindows()
        root=find_stage_window(api,'home',log)
        if not pid_matches(root['pid'],exe):
            raise WDMSError('NAVIGATION_PROCESS_MISMATCH','主窗口不属于配置的 WDMS，未点击。')
        number=[0]
        def scan(stage):
            number[0]+=1
            path=log.with_name(f'{log.stem}_scan_{number[0]:03d}.jsonl')
            probe.record(log,'navigation_scan',stage=stage,scan_file=str(path))
            try:
                probe.check_native_controls(api,root,path,max_depth=8,budget=5,read_labels=True)
                return supplement_navigation_captions(api,root,probe.read_events(path),path)
            except Exception as error:
                probe.record(path, 'native_scan_failed', **probe.error_info(error))
                raise
        cancel_file=getattr(args,'cancel_file',None)
        navigate(api,root,log,scan,on_missing_home_menu=lambda:resolve_home_menu(
            root,log,cancel_file),cancel_file=cancel_file)
        probe.record(log,'automatic_navigation_complete',query_performed=False,collection_performed=False)
        return 0
    except Exception as error:
        probe.record(log,'probe_error',**probe.error_info(error))
        return 2


def main(argv=None):
    parser=argparse.ArgumentParser(description='从已登录主页进入在库商品检索，不查询、不采集。')
    parser.add_argument('--_child',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--log',help=argparse.SUPPRESS)
    parser.add_argument('--out',type=Path,default=settings_path().parent/'navigation_logs')
    parser.add_argument('--cancel-file',type=Path,default=None)
    parser.add_argument('--execution-deadline',type=float,default=None)
    args=parser.parse_args(argv)
    if args.execution_deadline is not None and not math.isfinite(args.execution_deadline):
        parser.error('--execution-deadline 必须是有限的 monotonic 绝对期限。')
    if args._child:return child(args)
    args.out.mkdir(parents=True,exist_ok=True)
    log=(args.out/f'auto_navigation_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl').resolve()
    probe.record(log,'automatic_navigation_start',revision=NAVIGATION_REVISION)
    entry=Path(__file__).resolve().parents[1]/'scripts'/'WDMS_Demo.py'
    command=([sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(entry)])+['--native-navigate','--_child','--log',str(log)]
    if args.cancel_file is not None:
        command.extend(['--cancel-file',str(args.cancel_file.resolve())])
    timeout=240
    if args.execution_deadline is not None:
        timeout=min(timeout,args.execution_deadline-time.monotonic())
    if args.cancel_file is None:
        code=probe.supervise(command,log,timeout)
    else:
        cancel_path=args.cancel_file.resolve()
        code=probe.supervise(command,log,timeout,cancelled=lambda:cancel_path.exists())
    print(f'导航测试日志：{log}',flush=True)
    return code
