"""Native clipping and one physical mouse click for sidebar navigation."""
from __future__ import annotations

import ctypes
import time

from desktop_demo.login import LoginNativeWindows
from errors import WDMSError
from scripts import check_wdms_window as probe


MAX_ANCESTORS = 16


def intersection(first, second):
    rect = [max(first[0], second[0]), max(first[1], second[1]),
            min(first[2], second[2]), min(first[3], second[3])]
    return rect if rect[0] < rect[2] and rect[1] < rect[3] else None


def same_window(first, second):
    return all(first.get(key) == second.get(key)
               for key in ('hwnd', 'pid', 'class_name', 'rect'))


def inspect_geometry(api, root, target, require_enabled=True):
    """Intersect every ancestor client area; WS_VISIBLE alone is insufficient."""
    chain = []
    seen = set()
    hwnd = target['hwnd']
    for unused in range(MAX_ANCESTORS):
        if not hwnd or hwnd in seen:
            raise WDMSError('NAVIGATION_ANCESTRY_INVALID', '菜单父窗口链不完整或出现循环，未点击。')
        seen.add(hwnd)
        live = api.metadata(hwnd)
        if live['pid'] != root['pid']:
            raise WDMSError('NAVIGATION_TARGET_CHANGED', '菜单父窗口所属进程变化，未点击。')
        if not chain and (live['class_name'] != target['class_name']
                          or live['rect'] != target['rect']):
            raise WDMSError('NAVIGATION_TARGET_CHANGED', '菜单控件身份或位置变化，未点击。')
        node = {key: live[key] for key in ('hwnd', 'pid', 'class_name', 'rect', 'visible')}
        node['client_rect'] = api.client_screen_rect(hwnd)
        node['enabled'] = bool(api.user32.IsWindowEnabled(hwnd))
        node['minimized'] = bool(live.get('minimized', False))
        chain.append(node)
        if hwnd == root['hwnd']:
            break
        hwnd = int(api.user32.GetParent(hwnd) or 0)
    else:
        raise WDMSError('NAVIGATION_ANCESTRY_LIMIT', '菜单父窗口链超过检查上限，未点击。')

    visible_rect = list(target['rect'])
    clipped_by = None
    unavailable = None
    for node in chain:
        if (not node['visible'] or node['minimized']
                or (not node['enabled'] and (require_enabled or node['hwnd']!=target['hwnd']))):
            unavailable = node['hwnd']
        if visible_rect is not None:
            visible_rect = intersection(visible_rect, node['client_rect'])
            if visible_rect is None:
                clipped_by = node['hwnd']
    if unavailable is not None:
        visible_rect = None
    return {'chain': chain, 'visible_rect': visible_rect,
            'clipped_by': clipped_by, 'unavailable': unavailable}


def target_hit(api, geometry, point, log=None):
    """Permit transparent STATICs only through their verified ancestor path."""
    chain = geometry['chain']
    target = chain[0]
    hit = int(api.user32.WindowFromPoint(point) or 0)
    if log is not None:
        probe.record(log,'navigation_mouse_hit',target_hwnd=target['hwnd'],
                     hit_hwnd=hit,x=point.x,y=point.y)
    if hit == target['hwnd']:
        return True
    if not target['class_name'].startswith('WindowsForms10.STATIC.'):
        return False
    positions = [index for index, node in enumerate(chain) if node['hwnd'] == hit]
    if len(positions) != 1:
        return False
    for index in range(positions[0], 0, -1):
        parent, child = chain[index], chain[index - 1]
        client = probe.wintypes.POINT(point.x, point.y)
        if not api.user32.ScreenToClient(parent['hwnd'], ctypes.byref(client)):
            return False
        actual = int(api.user32.ChildWindowFromPointEx(parent['hwnd'], client, 3) or 0)
        if actual != child['hwnd']:
            return False
    return True


class NavigationNativeWindows(LoginNativeWindows):
    def __init__(self):
        super().__init__()
        definitions = {
            'GetClientRect': ([probe.wintypes.HWND, ctypes.POINTER(probe.wintypes.RECT)],
                              probe.wintypes.BOOL),
            'ClientToScreen': ([probe.wintypes.HWND, ctypes.POINTER(probe.wintypes.POINT)],
                               probe.wintypes.BOOL),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result

    def client_screen_rect(self, hwnd):
        rect = probe.wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise WDMSError('NAVIGATION_CLIENT_RECT_UNAVAILABLE', '无法取得菜单父窗口的客户区，未点击。')
        start = probe.wintypes.POINT(rect.left, rect.top)
        end = probe.wintypes.POINT(rect.right, rect.bottom)
        if (not self.user32.ClientToScreen(hwnd, ctypes.byref(start))
                or not self.user32.ClientToScreen(hwnd, ctypes.byref(end))):
            raise WDMSError('NAVIGATION_CLIENT_RECT_UNAVAILABLE', '无法换算菜单客户区的位置，未点击。')
        return [start.x, start.y, end.x, end.y]

    def click_navigation_input(self, root, target, log, before_press):
        u = self.user32
        self.health(root['hwnd'], log, 'before_navigation_mouse')
        u.SetForegroundWindow(root['hwnd'])
        deadline = time.monotonic() + 1
        while int(u.GetForegroundWindow() or 0) != root['hwnd']:
            if time.monotonic() >= deadline:
                raise WDMSError('NAVIGATION_FOREGROUND_UNCONFIRMED', 'WDMS未处于前台，未点击菜单。')
            time.sleep(0.02)

        geometry = inspect_geometry(self, root, target)
        if geometry != target['geometry'] or geometry['visible_rect'] is None:
            raise WDMSError('NAVIGATION_TARGET_CHANGED', '菜单父窗口、裁剪范围或可用状态变化，未点击。')
        area = geometry['visible_rect']
        if 'header_rect' in target:
            area = intersection(area, target['header_rect'])
        if area is None:
            raise WDMSError('NAVIGATION_TARGET_CLIPPED', '菜单被父窗口裁剪，未点击。')
        point = probe.wintypes.POINT((area[0] + area[2]) // 2, (area[1] + area[3]) // 2)
        probe.record(log,'navigation_mouse_geometry',hwnd=target['hwnd'],
                     visible_rect=geometry['visible_rect'],ancestors=geometry['chain'])
        if not target_hit(self, geometry, point, log):
            raise WDMSError('NAVIGATION_TARGET_OBSCURED', '菜单点击位置被遮挡，未点击。')
        if any(u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18)):
            raise WDMSError('NAVIGATION_INPUT_BUSY', '鼠标按钮或修饰键按住，未点击菜单。')
        if not u.SetCursorPos(point.x, point.y):
            raise WDMSError('NAVIGATION_INPUT_FAILED', '无法将鼠标移至菜单，未点击。')
        time.sleep(0.2)
        if not before_press():
            probe.record(log, 'navigation_mouse_cancelled', reason='group_already_expanded')
            return False
        actual = probe.wintypes.POINT()
        if (not u.GetCursorPos(ctypes.byref(actual))
                or (actual.x, actual.y) != (point.x, point.y)
                or int(u.GetForegroundWindow() or 0) != root['hwnd']
                or inspect_geometry(self, root, target) != geometry
                or not target_hit(self, geometry, actual, log)
                or any(u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18))):
            raise WDMSError('NAVIGATION_TARGET_CHANGED', '输入前菜单位置、裁剪、前台或输入状态变化，未点击。')
        probe.record(log, 'before_navigation_mouse_input', hwnd=target['hwnd'],
                     x=point.x, y=point.y, strategy='native_mouse_single_click',
                     visible_rect=geometry['visible_rect'], ancestors=geometry['chain'])
        if not before_press():
            probe.record(log, 'navigation_mouse_cancelled', reason='group_already_expanded')
            return False
        # Logging can retry after a sharing violation. Recheck after it, with
        # logging disabled, so no I/O remains between the guard and SendInput.
        if (not u.GetCursorPos(ctypes.byref(actual))
                or (actual.x, actual.y) != (point.x, point.y)
                or inspect_geometry(self, root, target) != geometry
                or not target_hit(self, geometry, actual)
                or any(u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18))
                or int(u.GetForegroundWindow() or 0) != root['hwnd']):
            raise WDMSError('NAVIGATION_TARGET_CHANGED', '日志记录后菜单目标或前台变化，未点击。')
        inputs = (probe.NativeInput * 2)()
        inputs[0].mi.dwFlags = 0x0002
        inputs[1].mi.dwFlags = 0x0004
        sent = u.SendInput(2, inputs, ctypes.sizeof(probe.NativeInput))
        if sent != 2:
            if sent == 1:
                release = probe.NativeInput()
                release.mi.dwFlags = 0x0004
                u.SendInput(1, ctypes.byref(release), ctypes.sizeof(probe.NativeInput))
            probe.record(log, 'navigation_mouse_input_sent', requested=2, sent=sent,
                         automatic_retries=False)
            raise WDMSError('NAVIGATION_INPUT_FAILED', '菜单鼠标输入未完整发送，停止且不重复点击。')
        probe.record(log, 'navigation_mouse_input_sent', requested=2, sent=sent,
                     automatic_retries=False)
        return True
