"""Native startup/login unit. One submit; no UIA, credential logging, navigation."""
from __future__ import annotations
import argparse
import ctypes
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from desktop_demo.settings import load_settings, settings_path, validate_executable
from desktop_demo.credentials import WindowsCredentials
from scripts import check_wdms_window as probe
from scripts.check_wdms_startup import find_stage_window
from scripts.run_native_trial import TrialNativeWindows
from errors import WDMSError
from adapters.native_windows import message_policy
from typing import Callable, Optional
from types import SimpleNamespace
from core.login_policy import DEFAULT_LOGIN_READINESS


class LoginNativeWindows(TrialNativeWindows):
    def __init__(self) -> None:
        super().__init__()
        definitions={
            'GetAncestor':([probe.wintypes.HWND,probe.wintypes.UINT],probe.wintypes.HWND),
            'ScreenToClient':([probe.wintypes.HWND,ctypes.POINTER(probe.wintypes.POINT)],probe.wintypes.BOOL),
            'ChildWindowFromPointEx':([probe.wintypes.HWND,probe.wintypes.POINT,probe.wintypes.UINT],probe.wintypes.HWND),
        }
        for name,(arguments,result) in definitions.items():
            getattr(self.user32,name).argtypes=arguments
            getattr(self.user32,name).restype=result

    def probe_login_ready(self, root: dict, log: Path) -> None:
        """One read-only probe; the startup loop owns all retry deadlines."""
        live = self.metadata(root['hwnd'])
        if (live['pid'] != root['pid'] or not live['visible'] or live.get('minimized', False)
                or ('class_name' in root and live['class_name'] != root['class_name'])):
            raise WDMSError('LOGIN_WINDOW_CHANGED', '登录窗口身份或可见状态变化，停止等待。')
        # Bypass TrialNativeWindows' nested 15-second retry: a single probe
        # stays within the shared cold-start budget and cannot send input.
        probe.NativeWindows.health(self, root['hwnd'], log, 'login_startup_ready')

    def click_query_input(self,root: dict,hwnd: int,log: Path,
                          before_press: Optional[Callable[[], bool]] = None) -> Optional[bool]:
        live=self.metadata(hwnd)
        if not live['class_name'].startswith(('WindowsForms10.STATIC.','WindowsForms10.Window.8.')):
            return super().click_query_input(root,hwnd,log)
        u=self.user32
        u.SetForegroundWindow(root['hwnd'])
        deadline=time.monotonic()+1
        while int(u.GetForegroundWindow() or 0)!=root['hwnd']:
            if time.monotonic()>=deadline:
                raise WDMSError('LOGIN_FOREGROUND_UNCONFIRMED','登录窗口未处于前台，未点击标签。')
            time.sleep(0.02)
        live=self.metadata(hwnd)
        if live['pid']!=root['pid'] or not live['visible'] or not u.IsWindowEnabled(hwnd):
            raise WDMSError('LOGIN_LABEL_CHANGED','标签状态变化，未点击。')
        expected_label={'class_name':live['class_name'],'rect':list(live['rect'])}
        left,top,right,bottom=live['rect']
        if right<=left or bottom<=top:
            raise WDMSError('LOGIN_LABEL_CHANGED','标签尺寸无效，未点击。')
        point=probe.wintypes.POINT((left+right)//2,(top+bottom)//2)
        def target_hit(location):
            hit=int(u.WindowFromPoint(location) or 0)
            if hit==hwnd:return True
            # Win32 can return the parent for transparent STATIC labels.
            if not hit or int(u.GetAncestor(hit,2) or 0)!=root['hwnd']:return False
            parent=int(u.GetParent(hwnd) or 0)
            if not parent or self.metadata(parent)['pid']!=root['pid']:return False
            client=probe.wintypes.POINT(location.x,location.y)
            return bool(u.ScreenToClient(parent,ctypes.byref(client))) and int(u.ChildWindowFromPointEx(parent,client,3) or 0)==hwnd
        if not target_hit(point):
            raise WDMSError('LOGIN_LABEL_OBSCURED','部门标签被遮挡，未点击。')
        if any(u.GetAsyncKeyState(key)&0x8000 for key in (1,2,4,16,17,18)):
            raise WDMSError('LOGIN_INPUT_BUSY','鼠标按钮或修饰键按住，未点击标签。')
        if not u.SetCursorPos(point.x,point.y):
            raise WDMSError('LOGIN_INPUT_FAILED','鼠标无法移至标签，未点击。')
        # Give the custom label time to process mouse-enter/activation before
        # pressing. A queued, instantaneous down/up did not toggle it on site.
        time.sleep(0.2)
        actual=probe.wintypes.POINT()
        if not u.GetCursorPos(ctypes.byref(actual)) or actual.x!=point.x or actual.y!=point.y or int(u.GetForegroundWindow() or 0)!=root['hwnd'] or not target_hit(actual):
            raise WDMSError('LOGIN_LABEL_CHANGED','点击前鼠标位置或前台窗口变化，未点击。')
        client=probe.wintypes.POINT(point.x,point.y)
        if not u.ScreenToClient(hwnd,ctypes.byref(client)):
            raise WDMSError('LOGIN_INPUT_FAILED','无法换算标签内部坐标，未点击。')
        # State may change while activating the window or hovering the label.
        # Recheck immediately before the one and only down message.
        if before_press is not None and not before_press():
            probe.record(log,'label_click_cancelled_already_selected',hwnd=hwnd)
            return False
        position=(client.x & 0xffff)|((client.y & 0xffff)<<16)
        probe.record(log,'before_label_mouse_input',hwnd=hwnd,x=point.x,y=point.y,
                     strategy='label_wm_mouse_single_click',client_x=client.x,client_y=client.y)
        if before_press is not None and not before_press():
            probe.record(log,'label_click_cancelled_already_selected',hwnd=hwnd)
            return False
        live=self.metadata(hwnd)
        if (live['pid']!=root['pid'] or live['class_name']!=expected_label['class_name']
                or not live['visible'] or live['rect']!=expected_label['rect']
                or not u.GetCursorPos(ctypes.byref(actual))
                or (actual.x,actual.y)!=(point.x,point.y) or not target_hit(actual)
                or any(u.GetAsyncKeyState(key)&0x8000 for key in (1,2,4,16,17,18))
                or int(u.GetForegroundWindow() or 0)!=root['hwnd']):
            raise WDMSError('LOGIN_LABEL_CHANGED','日志记录后部门标签或前台变化，未点击。')
        outcomes = []
        timeout_ms = self.message_policy.login_label_ms

        def send(message: int, buttons: int) -> dict:
            result=ctypes.c_size_t()
            # The ctypes last-error API exists on Windows; fake APIs also run
            # on Mac without it. Capture before another call can overwrite it.
            reset = getattr(ctypes, 'set_last_error', None)
            if reset is not None:
                reset(0)
            started = time.monotonic()
            ok=u.SendMessageTimeoutW(hwnd,message,buttons,position,0x23,timeout_ms,ctypes.byref(result))
            get_error = getattr(ctypes, 'get_last_error', lambda: 0)
            winerror = get_error() if not ok else 0
            outcome = {'hwnd': hwnd, 'message': message, 'delivered': bool(ok),
                       'winerror': winerror, 'timeout_ms': timeout_ms,
                       'elapsed_ms': round((time.monotonic()-started)*1000, 3)}
            outcomes.append(outcome)
            return outcome
        # Send only one down/up pair to the identified label. Always attempt
        # release even if down delivery is uncertain; never repeat the down.
        failure = None
        try:
            pressed = send(0x0201,1)
            if not pressed['delivered']:
                raise WDMSError('LOGIN_INPUT_FAILED',
                    f"标签按下消息未确认（WinError={pressed['winerror']}，等待上限{timeout_ms}ms），停止且不重复点击。")
            time.sleep(0.12)
        except BaseException as error:
            failure = error
        finally:
            try:
                released = send(0x0202,0)
                if not released['delivered'] and failure is None:
                    failure = WDMSError('LOGIN_INPUT_FAILED',
                        f"标签释放消息未确认（WinError={released['winerror']}），停止且不重复点击。")
            except BaseException as error:
                if failure is None:
                    failure = error
        # Complete the release before any diagnostic I/O or sharing retry.
        # Preserve an input failure even if its log cannot be written.
        for outcome in outcomes:
            try:
                probe.record(log,'label_mouse_message',**outcome)
            except OSError as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure
        probe.record(log,'label_mouse_input_sent',sent=2,strategy='label_wm_mouse_single_click')
        return True



def same_exe(first,second):
    return os.path.normcase(os.path.abspath(first))==os.path.normcase(os.path.abspath(second))


def normalized(text):
    return re.sub(r'\s+', '', re.sub(r'\([A-Za-z]\)', '', text.replace('&', ''))).rstrip(':：')


def map_login_controls(api,events,log=None):
    nodes=[e for e in events if e['event']=='native_control' and e.get('visible')]
    fields=[e for e in nodes if re.match(r'^(?:WindowsForms10\.)?EDIT(?:\.|$)',e['class_name'],re.I)]
    passwords=[];plain=[]
    for item in fields:
        # Style is metadata only; never EM_GETTEXT on password/username EDITs.
        style=api.user32.GetWindowLongW(item['hwnd'],-16)
        if log is not None:
            probe.record(log,'login_input_metadata',hwnd=item['hwnd'],class_name=item['class_name'],rect=item['rect'],password_style=bool(style & 0x20),native_style=style)
        if style & 0x20:
            passwords.append(item)
        else:plain.append(item)
    if len(passwords)!=1:
        raise WDMSError('LOGIN_PASSWORD_CONTROL_UNCONFIRMED','未识别到唯一的密码输入框，未读取凭据、未填写、未登录。请上传本轮日志。')
    texts=[e for e in events if e['event']=='native_text']
    labels=[e for e in texts if 'STATIC' in e['class_name'] and normalized(e['text']) in {'用户名','用户','账号','帐号','用户代码','操作员','操作员代码','登录账号','用户名称'}]
    candidates=plain+[e for e in nodes if e['class_name'].startswith('WindowsForms10.COMBOBOX.')]
    username=None
    if len(labels)==1:
        rect=labels[0]['rect']
        matches=[e for e in candidates if e['rect'][0]>=rect[2] and abs(e['rect'][1]+e['rect'][3]-rect[1]-rect[3])<=16]
        if matches:
            nearest=min(e['rect'][0] for e in matches)
            matches=[e for e in matches if e['rect'][0]==nearest]
            if len(matches)==1:username=matches[0]
    if username is None and len(plain)==1:
        username=plain[0]
    if username is None:
        raise WDMSError('LOGIN_USERNAME_CONTROL_UNCONFIRMED','账号输入框无法唯一识别，未读取凭据、未登录。请上传本轮日志。')
    buttons=[e for e in texts if e['class_name'].startswith('WindowsForms10.BUTTON.') and normalized(e['text']) in {'登录','登入','确定'}]
    if len(buttons)!=1:
        raise WDMSError('LOGIN_SUBMIT_CONTROL_UNCONFIRMED','登录按钮无法唯一识别，未读取凭据、未登录。请上传本轮日志。')
    return {'username':username,'password':passwords[0],'submit':buttons[0]}


def department_labels(events):
    labels={}
    for name in ('管理部','仓储部','运输部'):
        matches=[e for e in events if e['event']=='native_text'
                 and e['class_name'].startswith('WindowsForms10.STATIC.')
                 and normalized(e['text'])==name and e.get('visible',True)]
        if len(matches)!=1:
            raise WDMSError('LOGIN_DEPARTMENT_LABEL_UNCONFIRMED',f'无法识别唯一的{name}标签，未点击、未提交登录。')
        labels[name]=matches[0]
    if len(set(e['hwnd'] for e in labels.values()))!=3:
        raise WDMSError('LOGIN_DEPARTMENT_LABEL_UNCONFIRMED','三个标签不是三个独立控件。')
    return labels


def wait_department_states(read_states: Callable[[], tuple], log: Path,
                           minimum_seconds: float = DEFAULT_LOGIN_READINESS.department_minimum_seconds,
                           stable_seconds: float = DEFAULT_LOGIN_READINESS.department_stable_seconds,
                           timeout: float = DEFAULT_LOGIN_READINESS.startup_seconds, *,
                           ready: Optional[Callable[[], None]] = None,
                           deadline: Optional[float] = None, phase: str = 'department') -> tuple:
    start=time.monotonic()
    deadline=min(start+timeout, deadline) if deadline is not None else start+timeout
    previous=None
    stable_since=None
    responsive_since=start if ready is None else None
    blocked=False
    last_wait=None
    last_error=None
    probe.record(log,f'{phase}_settle_started',minimum_seconds=minimum_seconds,
                 stable_seconds=stable_seconds,timeout_seconds=max(0.0,deadline-start),
                 requires_responsive_window=ready is not None)
    while time.monotonic()<deadline:
        if ready is not None:
            try:
                ready()
            except WDMSError as error:
                if error.code not in ('WDMS_HEALTH_CHECK_FAILED','WDMS_UNRESPONSIVE'):
                    raise
                now=time.monotonic()
                if not blocked or last_wait is None or now-last_wait>=5:
                    probe.record(log,f'{phase}_ui_wait',elapsed_seconds=round(now-start,3),
                                 **probe.error_info(error))
                    last_wait=now
                previous=None
                stable_since=None
                responsive_since=None
                blocked=True
                last_error=error
                time.sleep(max(0.0,min(DEFAULT_LOGIN_READINESS.poll_seconds,deadline-time.monotonic())))
                continue
            now=time.monotonic()
            if now>=deadline:
                break
            if responsive_since is None:
                responsive_since=now
            if blocked:
                probe.record(log,f'{phase}_ui_recovered',elapsed_seconds=round(now-start,3))
            blocked=False
        states=read_states()
        now=time.monotonic()
        if now>=deadline:
            break
        if states!=previous:
            probe.record(log,f'{phase}_settle_state',states=list(states))
            previous=states
            stable_since=now
        if now-responsive_since>=minimum_seconds and now-stable_since>=stable_seconds:
            probe.record(log,f'{phase}_settle_complete',states=list(states),elapsed_seconds=round(now-start,3))
            return states
        time.sleep(max(0.0,min(DEFAULT_LOGIN_READINESS.poll_seconds,deadline-time.monotonic())))
    if ready is not None and (blocked or responsive_since is None):
        raise WDMSError('LOGIN_WINDOW_NOT_READY','登录窗口在等待期限内仍未恢复响应，停止后续操作。') from last_error
    raise WDMSError('LOGIN_DEPARTMENT_STATE_UNSTABLE','部门状态未稳定，未继续点击或提交登录。')


def wait_login_window_ready(api, root: dict, log: Path, *, deadline: float) -> None:
    wait_department_states(lambda: (), log,
        minimum_seconds=DEFAULT_LOGIN_READINESS.window_stable_seconds,
        stable_seconds=DEFAULT_LOGIN_READINESS.window_stable_seconds,
        ready=lambda: api.probe_login_ready(root, log), deadline=deadline, phase='login_window')


def click_department_labels(api,root,events,log, *, startup_deadline: Optional[float] = None):
    labels=department_labels(events)
    combos=[e for e in events if e['event']=='native_control' and e.get('visible')
            and e['class_name'].startswith('WindowsForms10.COMBOBOX.')]
    pairs={}
    for name,label in labels.items():
        rect=label['rect']
        matches=[e for e in combos if e['rect'][0]>=rect[2]
                 and abs(e['rect'][1]+e['rect'][3]-rect[1]-rect[3])<=16]
        if matches:
            nearest=min(e['rect'][0] for e in matches)
            matches=[e for e in matches if e['rect'][0]==nearest]
        if len(matches)!=1:
            raise WDMSError('LOGIN_DEPARTMENT_STATE_UNCONFIRMED',f'{name}右侧下拉框无法唯一识别，未点击部门标签。')
        pairs[name]=(label,matches[0])
    if len({combo['hwnd'] for label,combo in pairs.values()})!=3:
        raise WDMSError('LOGIN_DEPARTMENT_STATE_UNCONFIRMED','部门下拉框不是三个独立控件，未点击。')

    def selected(name):
        label,combo=pairs[name]
        for item in (label,combo):
            live=api.metadata(item['hwnd'])
            if live['pid']!=root['pid'] or live['class_name']!=item['class_name'] or not live['visible'] or live['rect']!=item['rect']:
                raise WDMSError('LOGIN_DEPARTMENT_LABEL_CHANGED','部门标签或下拉框身份、位置变化，停止登录。')
        # Confirmed on site: blue label => corresponding combo enabled.
        # Use native enabled state instead of guessing screenshot colours.
        return bool(api.user32.IsWindowEnabled(combo['hwnd']))

    def read_states():
        return tuple(selected(name) for name in pairs)
    ready=lambda: api.probe_login_ready(root,log)
    # A window can appear before the saved department state is restored.
    # Let startup settle before interpreting disabled combos as unselected.
    states=wait_department_states(read_states,log,ready=ready,deadline=startup_deadline)
    for name,state in zip(pairs,states):
        probe.record(log,'department_selection_state',label=name,selected=state,combo_hwnd=pairs[name][1]['hwnd'])
    for name,(item,combo) in pairs.items():
        if selected(name):
            probe.record(log,'department_selection_preserved',label=name)
            continue
        probe.record(log,'before_department_label_click',label=name,hwnd=item['hwnd'])
        clicked=api.click_query_input(root,item['hwnd'],log,before_press=lambda: not selected(name))
        probe.record(log,'department_selection_preserved' if clicked is False else 'department_label_clicked',label=name,hwnd=item['hwnd'])
        deadline=time.monotonic()+15
        while not selected(name):
            if time.monotonic()>=deadline:
                raise WDMSError('LOGIN_DEPARTMENT_SELECTION_UNCONFIRMED',f'{name}点击后下拉框仍不可用，停止登录，未重复点击。')
            time.sleep(0.1)
        probe.record(log,'department_selection_confirmed',label=name,combo_hwnd=combo['hwnd'])
        # Wait for dependent department updates before examining the next one.
        wait_department_states(read_states,log,minimum_seconds=0,stable_seconds=2,
                               timeout=DEFAULT_LOGIN_READINESS.post_change_seconds,ready=ready)
    if not all(wait_department_states(read_states,log,minimum_seconds=0,stable_seconds=2,
                                     timeout=DEFAULT_LOGIN_READINESS.post_change_seconds,ready=ready)):
        raise WDMSError('LOGIN_DEPARTMENT_SELECTION_CHANGED','填写账号前部门选中状态发生变化，停止登录。')
    probe.record(log,'all_departments_selected',verified_by='native_combo_enabled')


def set_field(api,item,value,pid):
    live=api.metadata(item['hwnd'])
    if live['pid']!=pid or live['class_name']!=item['class_name'] or not live['visible'] or not api.user32.IsWindowEnabled(item['hwnd']):
        raise WDMSError('LOGIN_CONTROL_CHANGED','填写前登录控件状态变化，停止登录。')
    buffer=ctypes.create_unicode_buffer(value)
    result=ctypes.c_size_t()
    try:
        if not api.user32.SendMessageTimeoutW(item['hwnd'],0x000C,0,ctypes.cast(buffer,ctypes.c_void_p).value,0x23,message_policy(api).field_write_ms,ctypes.byref(result)) or not result.value:
            raise WDMSError('LOGIN_FIELD_WRITE_FAILED','登录字段填写未完成，停止且不提交登录。')
    finally:
        ctypes.memset(ctypes.addressof(buffer),0,ctypes.sizeof(buffer))


class GuiThreadInfo(ctypes.Structure):
    _fields_=[('cbSize',probe.wintypes.DWORD),('flags',probe.wintypes.DWORD),
              ('hwndActive',probe.wintypes.HWND),('hwndFocus',probe.wintypes.HWND),
              ('hwndCapture',probe.wintypes.HWND),('hwndMenuOwner',probe.wintypes.HWND),
              ('hwndMoveSize',probe.wintypes.HWND),('hwndCaret',probe.wintypes.HWND),
              ('rcCaret',probe.wintypes.RECT)]


def wait_field_focus(api,root,item,log):
    function=api.user32.GetGUIThreadInfo
    function.argtypes=[probe.wintypes.DWORD,ctypes.POINTER(GuiThreadInfo)]
    function.restype=probe.wintypes.BOOL
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        info=GuiThreadInfo();info.cbSize=ctypes.sizeof(info)
        if int(api.user32.GetForegroundWindow() or 0)!=root['hwnd']:
            raise WDMSError('LOGIN_FOREGROUND_CHANGED','填写期间登录窗口离开前台，停止登录。')
        if function(0,ctypes.byref(info)) and int(info.hwndFocus or 0)==item['hwnd']:
            probe.record(log,'login_field_focus_verified',hwnd=item['hwnd'])
            return
        time.sleep(0.05)
    raise WDMSError('LOGIN_FIELD_FOCUS_UNCONFIRMED','输入框焦点未确认，未提交登录。')


def prepare_fields(api,root,controls,username,password,log):
    # Actual focus/blur is needed by clients whose role-loading runs in Leave.
    api.click_query_input(root,controls['username']['hwnd'],log)
    wait_field_focus(api,root,controls['username'],log)
    set_field(api,controls['username'],username,root['pid'])
    api.click_query_input(root,controls['password']['hwnd'],log)
    wait_field_focus(api,root,controls['password'],log)
    set_field(api,controls['password'],password,root['pid'])
    probe.record(log,'login_fields_prepared',credential_values_logged=False)


def wait_submit_enabled(api,root,controls,events,log,timeout=30):
    deadline=time.monotonic()+timeout
    button=controls['submit']['hwnd']
    previous=None
    last_combos=None
    next_combos=0
    combos=[e for e in events if e['event']=='native_text' and e['class_name'].startswith('WindowsForms10.COMBOBOX.')]
    while time.monotonic()<deadline:
        live=api.metadata(button)
        if live['pid']!=root['pid'] or live['class_name']!=controls['submit']['class_name'] or not live['visible']:
            raise WDMSError('LOGIN_SUBMIT_CHANGED','登录按钮身份或可见状态变化，未提交登录。')
        enabled=bool(api.user32.IsWindowEnabled(button))
        if enabled!=previous:
            probe.record(log,'login_submit_state',enabled=enabled)
            previous=enabled
        if time.monotonic()>=next_combos:
            values=[]
            for combo in combos:
                try:
                    current=api.metadata(combo['hwnd'])
                    if current['pid']!=root['pid'] or current['class_name']!=combo['class_name'] or not current['visible']:
                        raise WDMSError('LOGIN_DEPARTMENT_CHANGED','登录部门下拉框身份变化，停止登录。')
                    values.append({'hwnd':combo['hwnd'],'text':api.read_label_text({**combo,**current},log)})
                except WDMSError as error:
                    if error.code!='NATIVE_TEXT_READ_FAILED':raise
                    values.append({'hwnd':combo['hwnd'],'read_unavailable':True})
            if values!=last_combos:
                probe.record(log,'login_department_state',combos=values)
                last_combos=values
            next_combos=time.monotonic()+0.5
        if enabled:
            probe.record(log,'login_submit_ready')
            return
        time.sleep(0.1)
    raise WDMSError('LOGIN_SUBMIT_DISABLED','填写后等待 30 秒，登录按钮仍不可用。可能需要部门选择或账号校验；请查看窗口并上传日志。未提交登录。')


def pid_matches(pid,exe):
    import psutil
    try:return same_exe(psutil.Process(pid).exe(),exe)
    except (psutil.Error,OSError):return False


def logged_in_home_markers(events):
    markers=[e['text'] for e in events if e['event']=='native_text' and e.get('visible',True)
             and normalized(e['text']) in {'在库管理','在库查询','在库商品检索'}]
    # On-site home shows the authenticated account menu rather than stock
    # navigation text. Require the visible MDI workspace as additional evidence.
    mdi=any(e['event']=='native_control' and e.get('visible') and e['class_name'].startswith('WindowsForms10.MDICLIENT.') for e in events)
    if mdi:
        markers.extend(e['text'] for e in events if e['event']=='native_text' and e.get('visible',True)
                       and e['class_name'].startswith('WindowsForms10.STATIC.') and normalized(e['text'])=='用户密码修改')
    return markers


def confirm_existing_session(exe, expected_pid, log):
    """Reuse only a live authenticated workspace; never focus or enter credentials."""
    from desktop_demo import navigation
    api = navigation.NavigationNativeWindows()
    root = find_stage_window(api, 'home', log)
    if (root['pid'] != expected_pid or not pid_matches(root['pid'], exe)
            or not root['class_name'].startswith('WindowsForms10.Window.8.')
            or not api.user32.IsWindowEnabled(root['hwnd'])):
        raise WDMSError('LOGIN_EXISTING_SESSION_UNCONFIRMED', '现有WDMS窗口身份或可用状态无法确认，未复用、未填写凭据。')
    api.health(root['hwnd'], log, 'existing_session')
    home_log = log.with_name(log.stem + '_home.jsonl')
    probe.check_native_controls(api, root, home_log, max_depth=8, budget=25, read_labels=True)
    events = navigation.supplement_navigation_captions(api, root, probe.read_events(home_log), home_log)
    evidence = None
    if navigation.confirm_inventory_page(api, root, events, home_log, already_open=True):
        evidence = 'inventory_page'
    else:
        names = {normalized(text) for text in logged_in_home_markers(events)}
        for item in events:
            if item['event'] != 'native_text' or normalized(item.get('text', '')) not in names:
                continue
            live = api.metadata(item['hwnd'])
            if (live['pid'] == root['pid'] and live['class_name'] == item['class_name']
                    and normalized(navigation.navigation_caption(api, live, home_log)) == normalized(item['text'])
                    and navigation.inspect_geometry(api, root, {**item, **live})['visible_rect'] is not None):
                evidence = 'visible_home_marker'
                break
        if evidence is None:
            # The supplied home UI has an employee link inside the enterprise
            # sidebar. Require that actual parent relationship and clipping.
            links = [item for item in events if item['event'] == 'native_text'
                     and item['class_name'].startswith('WindowsForms10.STATIC.')
                     and normalized(item.get('text', '')) == '员工与用户']
            if len(links) == 1:
                item = links[0]
                live = api.metadata(item['hwnd'])
                if (live['pid'] != root['pid'] or live['class_name'] != item['class_name']
                        or normalized(navigation.navigation_caption(api, live, home_log)) != '员工与用户'):
                    raise WDMSError('LOGIN_EXISTING_SESSION_UNCONFIRMED', '现有主页标签身份或文字变化，未复用。')
                geometry = navigation.inspect_geometry(api, root, {**item, **live})
                if geometry['visible_rect'] is not None:
                    for parent in geometry['chain'][1:-1]:
                        if (parent['class_name'].startswith('WindowsForms10.Window.8.')
                                and normalized(navigation.navigation_caption(api, parent, home_log)) == '企业管理'):
                            evidence = 'enterprise_sidebar'
                            break
    if evidence is None:
        raise WDMSError('LOGIN_EXISTING_SESSION_UNCONFIRMED', '未确认现有WDMS的已登录页面，停止且不重复登录。')
    live = api.metadata(root['hwnd'])
    if (live['pid'] != expected_pid or live['class_name'] != root['class_name']
            or not live['visible'] or live['minimized'] or not pid_matches(expected_pid, exe)
            or not api.user32.IsWindowEnabled(root['hwnd'])):
        raise WDMSError('LOGIN_EXISTING_SESSION_UNCONFIRMED', '复用前主窗口状态变化，停止。')
    try:
        find_stage_window(api, 'login', log)
    except WDMSError as error:
        if error.code != 'STARTUP_WINDOW_NOT_UNIQUE' or '当前找到 0 个' not in error.message:
            raise
    else:
        raise WDMSError('LOGIN_EXISTING_SESSION_UNCONFIRMED', '复用检查期间出现登录窗口，停止。')
    probe.record(log, 'existing_wdms_session_confirmed', main_hwnd=root['hwnd'], pid=expected_pid,
                 evidence=evidence, credentials_read=False, actions_performed=False,
                 account_independently_verified=False)
    probe.record(log, 'automatic_login_complete', main_hwnd=root['hwnd'], login_success_verified=True,
                 session_reused=True, login_performed=False, navigation_performed=False)
    return 0


def wait_login_completion(api, root: dict, exe: str, log: Path, *,
                          execution_deadline: Optional[float] = None,
                          cancel_file: Optional[Path] = None) -> tuple[dict, list[str]]:
    """Observe one submitted login; loading windows are a normal pending state."""
    start=time.monotonic()
    deadline=start+DEFAULT_LOGIN_READINESS.completion_seconds
    if execution_deadline is not None:
        deadline=min(deadline,execution_deadline)
    cancel=Path(cancel_file) if cancel_file is not None else None
    phase='login_pending'
    previous_phase=None
    last_progress=start-10
    scan_index=0
    next_scan=start
    previous_context=getattr(api,'phase_context',None)

    def check() -> None:
        if cancel is not None and cancel.exists():
            raise WDMSError('TASK_CANCELLED','登录结果等待已取消，未重复提交登录。')
        if previous_context is not None:
            previous_context.check()
        now=time.monotonic()
        if execution_deadline is not None and now>=execution_deadline:
            raise WDMSError('STEP_TIMEOUT','本轮登录执行期限已到，未重复提交登录。')
        if now>=deadline:
            if phase=='login_pending':
                code='LOGIN_NOT_COMPLETED'
                message='登录结果等待达到120秒上限，登录窗口仍可见；未重复提交，请查看登录或加载页面。'
            elif phase=='home_missing':
                code='LOGIN_HOME_WINDOW_TIMEOUT'
                message='登录窗口已消失，但120秒期限内未找到主窗口；未重复提交登录。'
            else:
                code='LOGIN_HOME_MARKER_UNCONFIRMED'
                message='主窗口已出现，但120秒期限内可用状态或首页标记未确认；未重复提交登录。'
            raise WDMSError(code,message)

    api.phase_context=SimpleNamespace(deadline=deadline,check=check)
    try:
        probe.record(log,'login_result_wait_started',timeout_seconds=DEFAULT_LOGIN_READINESS.completion_seconds,
                     deadline=deadline,automatic_retries=False)
        while True:
            check()
            if api.user32.IsWindow(root['hwnd']) and api.user32.IsWindowVisible(root['hwnd']):
                phase='login_pending'
            else:
                phase='home_missing'
                main=None
                try:
                    main=find_stage_window(api,'home',log)
                except WDMSError as error:
                    if error.code!='STARTUP_WINDOW_NOT_UNIQUE' or '当前找到 0 个' not in error.message:
                        raise
                check()
                if main is not None:
                    if main['pid']!=root['pid'] or not pid_matches(main['pid'],exe):
                        raise WDMSError('LOGIN_MAIN_WINDOW_MISMATCH','登录后主窗口身份无法确认。')
                    phase='home_pending'
                    if api.user32.IsWindowEnabled(main['hwnd']) and time.monotonic()>=next_scan:
                        scan_index+=1
                        scan_log=log.with_name(f'{log.stem}_scan_home_{scan_index:03d}.jsonl')
                        try:
                            api.probe_login_ready(main,scan_log)
                            check()
                            events=probe.check_native_controls(api,main,scan_log,max_depth=8,
                                budget=min(25.0,deadline-time.monotonic()),read_labels=True)
                            if events is None:
                                events=probe.read_events(scan_log)
                            check()
                            markers=logged_in_home_markers(events)
                            if markers:
                                live=api.metadata(main['hwnd'])
                                if (live['pid']!=root['pid'] or not live['visible']
                                        or ('class_name' in main and live['class_name']!=main['class_name'])):
                                    raise WDMSError('LOGIN_MAIN_WINDOW_MISMATCH','首页确认期间主窗口身份或可见状态变化。')
                                if api.user32.IsWindow(root['hwnd']) and api.user32.IsWindowVisible(root['hwnd']):
                                    phase='login_pending'
                                elif api.user32.IsWindowEnabled(main['hwnd']):
                                    check()
                                    home_log=log.with_name(log.stem+'_home.jsonl')
                                    home_log.write_bytes(scan_log.read_bytes())
                                    check()
                                    probe.record(log,'login_result_confirmed',elapsed_seconds=round(time.monotonic()-start,3),
                                                 main_hwnd=main['hwnd'],home_markers=markers)
                                    check()
                                    return main,markers
                        except WDMSError as error:
                            recoverable=error.code in ('WDMS_UNRESPONSIVE','WDMS_HEALTH_CHECK_FAILED','WDMS_HEALTH_WAIT_TIMEOUT')
                            recoverable=recoverable or (isinstance(error,probe.NativeTextReadError) and error.winerror==1460)
                            if not recoverable:
                                raise
                            probe.record(log,'login_result_read_pending',**probe.error_info(error))
                        next_scan=time.monotonic()+DEFAULT_LOGIN_READINESS.home_scan_interval_seconds
            now=time.monotonic()
            if phase!=previous_phase or now-last_progress>=10:
                probe.record(log,'login_result_wait',phase=phase,elapsed_seconds=round(now-start,3),
                             remaining_seconds=round(max(0.0,deadline-now),3),automatic_retries=False)
                previous_phase=phase
                last_progress=now
            check()
            time.sleep(max(0.0,min(DEFAULT_LOGIN_READINESS.completion_poll_seconds,deadline-time.monotonic())))
    finally:
        api.phase_context=previous_context


def child(args):
    log=Path(args.log)
    try:
        settings=load_settings()
        if settings is None:raise WDMSError('SETTINGS_MISSING','请先完成首次配置并保存账号密码。')
        exe=validate_executable(settings.wdms_exe)
        probe.require_usable_environment(probe.inspect_environment())
        import psutil
        running=[]
        for process in psutil.process_iter(['pid','exe']):
            if process.info['exe'] and same_exe(process.info['exe'],exe):running.append(process.info['pid'])
        if len(running)>1:raise WDMSError('WDMS_INSTANCE_AMBIGUOUS','存在多个已配置的 WDMS 客户端，请仅保留一个。')
        launched=False
        if not running:
            subprocess.Popen([exe],cwd=str(Path(exe).parent))
            launched=True
            probe.record(log,'wdms_launch_requested',executable=exe)
        api=LoginNativeWindows()
        deadline=time.monotonic()+45
        root=None
        while time.monotonic()<deadline:
            try:
                root=find_stage_window(api,'login',log)
                if not pid_matches(root['pid'],exe):
                    raise WDMSError('LOGIN_PROCESS_MISMATCH','登录窗口不属于配置的客户端，未填写凭据。')
                break
            except WDMSError as error:
                if error.code!='STARTUP_WINDOW_NOT_UNIQUE' or '当前找到 0 个' not in error.message:raise
                if not launched:
                    if getattr(args, 'reuse_session', False):
                        return confirm_existing_session(exe, running[0], log)
                    raise WDMSError('WDMS_ALREADY_RUNNING_NO_LOGIN','WDMS 已在运行但未显示登录窗口。此轮只测试自动登录，请关闭客户端后重新测试。') from error
                time.sleep(0.25)
        if root is None:raise WDMSError('WDMS_LOGIN_WINDOW_TIMEOUT','启动后 45 秒内未出现登录窗口。')
        startup_deadline=time.monotonic()+DEFAULT_LOGIN_READINESS.startup_seconds
        wait_login_window_ready(api,root,log,deadline=startup_deadline)
        probe.check_native_controls(api,root,log,max_depth=8,budget=25,read_labels=True)
        controls=map_login_controls(api,probe.read_events(log),log)
        probe.record(log,'login_controls_identified',username_hwnd=controls['username']['hwnd'],password_hwnd=controls['password']['hwnd'],submit_hwnd=controls['submit']['hwnd'])
        click_department_labels(api,root,probe.read_events(log),log,startup_deadline=startup_deadline)
        username,password=WindowsCredentials().read(settings.credential_target)
        if username!=settings.username or not password:
            raise WDMSError('LOGIN_CREDENTIAL_MISMATCH','本机凭据与配置账号不一致，请重新保存账号密码。')
        try:
            prepare_fields(api,root,controls,username,password,log)
        finally:password=''
        wait_submit_enabled(api,root,controls,probe.read_events(log),log)
        api.health(root['hwnd'],log,'before_login_submit')
        probe.record(log,'before_login_submit',credential_values_logged=False)
        api.click_query_input(root,controls['submit']['hwnd'],log)
        probe.record(log,'login_submitted',automatic_retries=False)
        main,markers=wait_login_completion(api,root,exe,log,
            execution_deadline=getattr(args,'execution_deadline',None),cancel_file=getattr(args,'cancel_file',None))
        probe.record(log,'automatic_login_complete',main_hwnd=main['hwnd'],home_markers=markers,
                     login_success_verified=True,session_reused=False,login_performed=True,
                     navigation_performed=False)
        return 0
    except Exception as error:
        # Error reporting intentionally omits arbitrary exception text/credentials.
        probe.record(log,'probe_error',**probe.error_info(error))
        return 2


def main(argv=None):
    parser=argparse.ArgumentParser(description='读取本机配置，启动 WDMS 并只提交一次登录；不导航、不采集。')
    parser.add_argument('--_child',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--log',help=argparse.SUPPRESS)
    parser.add_argument('--out',type=Path,default=settings_path().parent/'login_logs')
    parser.add_argument('--cancel-file',type=Path,default=None)
    parser.add_argument('--execution-deadline',type=float,default=None)
    parser.add_argument('--reuse-session',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.execution_deadline is not None and not math.isfinite(args.execution_deadline):
        parser.error('--execution-deadline 必须是有限的 monotonic 绝对期限。')
    if args._child:return child(args)
    args.out.mkdir(parents=True,exist_ok=True)
    log=(args.out/f'auto_login_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl').resolve()
    probe.record(log,'automatic_login_start',credential_values_logged=False,
                 reuse_session_allowed=args.reuse_session,
                 login_revision='department-settle-home-markers-v10-network-wait')
    if getattr(sys,'frozen',False):command=[sys.executable,'--native-login','--_child','--log',str(log)]
    else:command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts'/'WDMS_Demo.py'),'--native-login','--_child','--log',str(log)]
    if args.reuse_session:
        command.append('--reuse-session')
    if args.execution_deadline is not None:
        command.extend(['--execution-deadline',str(args.execution_deadline)])
    if args.cancel_file is not None:
        command.extend(['--cancel-file',str(args.cancel_file.resolve())])
    timeout=DEFAULT_LOGIN_READINESS.worker_seconds
    if args.execution_deadline is not None:
        timeout=min(timeout,args.execution_deadline-time.monotonic())
    if args.cancel_file is None:
        code=probe.supervise(command,log,timeout)
    else:
        cancel_path=args.cancel_file.resolve()
        code=probe.supervise(command,log,timeout,cancelled=lambda:cancel_path.exists())
    for e in probe.read_events(log):
        if e.get('error_code') or e['event']=='automatic_login_complete':print(__import__('json').dumps(e,ensure_ascii=False),flush=True)
    print(f'登录测试日志：{log}',flush=True)
    return code
