"""Match the approved icon in a verified toolbar; no OCR or network calls."""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import tempfile
import time
from pathlib import Path

from errors import WDMSError
from scripts import check_wdms_window as probe
from scripts.diagnose_wdms_menu import _powershell_executable

TARGET = '在库管理'
TEMPLATE_SHA256 = '46549712f44e97f522167ead3113947ed3df61a8bd90b02caa7344eb8ff3fd8f'
MATCH_REVISION = 'toolbar-icon-v2'
MIN_SCORE = 0.86
MIN_MARGIN = 0.06


def fail(code, message):
    raise WDMSError(code, message)


def choose_toolbar(request, events, root):
    if request.get('root_hwnd') != root['hwnd'] or request.get('pid') != root['pid']:
        fail('ICON_ROOT_CHANGED', '诊断时的 WDMS 与当前窗口不同，未识别或点击。')
    candidates = []
    for item in request.get('containers', []):
        summaries = [e for e in events if e.get('event') == 'menu_msaa_container_complete'
                     and e.get('container_hwnd') == item.get('hwnd')]
        if len(summaries) != 1 or summaries[0].get('root_role') != 22:
            continue
        if summaries[0].get('truncated', False) is not False:
            continue
        left, top, right, bottom = item['rect']
        rl, rt, rr, rb = root['rect']
        if (24 <= bottom-top <= 160 and (rr-rl)/2 <= right-left <= 4096
                and rl <= left < right <= rr and rt <= top < bottom <= min(rb, rt+180)):
            candidates.append({**item, 'pid': root['pid'], 'visible': True})
    if len(candidates) != 1:
        fail('ICON_TOOLBAR_AMBIGUOUS', '没有唯一、完整确认的顶部工具栏，未识别或点击。')
    return candidates[0]


def target_from_match(data, toolbar):
    fields = {'revision', 'template_id', 'rect', 'score', 'runner_up', 'scale_height',
              'elapsed_ms', 'button_rect', 'button_state', 'button_role'}
    if (not isinstance(data, dict) or set(data) != fields
            or data['revision'] != MATCH_REVISION or type(data['template_id']) is not int
            or data['template_id'] != 18):
        fail('ICON_RESULT_INVALID', '本机图标匹配结果格式无效。')
    for key in ('score', 'runner_up'):
        value = data[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not -1 <= value <= 1):
            fail('ICON_RESULT_INVALID', '图标匹配分数无效。')
    if (type(data['scale_height']) is not int or not 20 <= data['scale_height'] <= 112
            or type(data['elapsed_ms']) is not int or not 0 <= data['elapsed_ms'] <= 5000):
        fail('ICON_RESULT_INVALID', '图标匹配尺寸或耗时无效。')
    rect, button = data['rect'], data['button_rect']
    for box in (rect, button):
        if not isinstance(box, list) or len(box) != 4 or any(type(x) is not int for x in box):
            fail('ICON_RESULT_INVALID', '图标或按钮矩形无效。')
    left, top, right, bottom = toolbar['rect']
    x1, y1, x2, y2 = rect
    h = data['scale_height']; w = round(h*142/130)
    if (not 0 <= x1 < x2 <= right-left or not 0 <= y1 < y2 <= bottom-top
            or x2-x1 != w-2*int(w*.16) or y2-y1 != h-2*int(h*.12)):
        fail('ICON_RESULT_INVALID', '图标位置越出工具栏或尺寸不符，未点击。')
    if data['score'] < MIN_SCORE:
        fail('ICON_LOW_CONFIDENCE', '18号图标匹配分数不足，未猜测位置或点击。')
    if data['score']-data['runner_up'] < MIN_MARGIN:
        fail('ICON_TARGET_NOT_UNIQUE', '18号图标没有唯一匹配，未点击。')
    absolute = [left+x1, top+y1, left+x2, top+y2]
    state = data['button_state']
    if (type(state) is not int or not 0 <= state <= 0x3fffffff
            or type(data['button_role']) is not int or data['button_role'] != 43
            or state & (1 | 32768 | 65536)
            or not left <= button[0] <= absolute[0] < absolute[2] <= button[2] <= right
            or not top <= button[1] <= absolute[1] < absolute[3] <= button[3] <= bottom):
        fail('ICON_BUTTON_UNCONFIRMED', '未确认图标属于可用的顶部菜单按钮，未点击。')
    return {**toolbar, 'icon_rect': absolute, 'button_rect': button, 'strategy': 'toolbar_icon'}


def capture_target(root, toolbar, log, cancel_file=None):
    if cancel_file is not None and Path(cancel_file).exists():
        fail('TASK_CANCELLED', '任务已停止，未识别或点击。')
    base = Path(__file__).resolve().parents[1]
    template = base/'assets'/'toolbar_inventory_18.png'
    if (not template.is_file() or template.stat().st_size > 128*1024
            or hashlib.sha256(template.read_bytes()).hexdigest() != TEMPLATE_SHA256):
        fail('ICON_TEMPLATE_INVALID', '18号原始图标缺失或校验失败，未点击。')
    # The bridge keeps the cropped screen in RAM; only scores/geometry leave it.
    with tempfile.TemporaryDirectory(prefix='wdms_toolbar_') as folder:
        output = Path(folder)/'match.json'
        script = base/'scripts'/'wdms_toolbar_icon.ps1'
        command = [str(_powershell_executable()), '-NoProfile', '-NonInteractive', '-STA',
                   '-ExecutionPolicy', 'Bypass', '-File', str(script),
                   '-RootHwnd', str(root['hwnd']), '-ProcessId', str(root['pid']),
                   '-ContainerHwnd', str(toolbar['hwnd']),
                   '-ExpectedRect', ','.join(str(x) for x in toolbar['rect']),
                   '-TemplateFile', str(template), '-OutputFile', str(output)]
        cancelled = (lambda: Path(cancel_file).exists()) if cancel_file is not None else None
        code = probe.supervise(command, log, 20, cancelled=cancelled) if cancelled else probe.supervise(command, log, 20)
        if code != 0 or not output.is_file():
            fail('ICON_UNAVAILABLE', '本机图标匹配不可用、超时或已取消，未点击。')
        if output.stat().st_size > 4096:
            fail('ICON_RESULT_INVALID', '本机图标匹配输出超过限制。')
        try:
            data = json.loads(output.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            fail('ICON_RESULT_INVALID', '本机图标匹配输出无效。')
        if isinstance(data, dict) and 'error_code' in data:
            known = {'ICON_CONTAINER_CHANGED', 'ICON_FOREGROUND_UNCONFIRMED', 'ICON_IMAGE_INVALID', 'ICON_BRIDGE_COMPILE_FAILED',
                     'ICON_MATCH_TIMEOUT', 'ICON_TARGET_NOT_FOUND', 'ICON_BUTTON_UNCONFIRMED', 'ICON_TARGET_OBSCURED'}
            code = data['error_code'] if isinstance(data['error_code'], str) and data['error_code'] in known else 'ICON_UNAVAILABLE'
            stages = {'assembly_load', 'source_read', 'compile', 'capture_match'}
            stage = data.get('stage')
            probe.record(log, 'toolbar_icon_bridge_failed', code=code,
                         stage=stage if isinstance(stage, str) and stage in stages else 'unknown')
            fail(code, '本机图标匹配停止，未点击；错误类型已记录。')
        try:
            target = target_from_match(data, toolbar)
        except WDMSError as error:
            if error.code in {'ICON_LOW_CONFIDENCE', 'ICON_TARGET_NOT_UNIQUE', 'ICON_BUTTON_UNCONFIRMED'}:
                probe.record(log, 'toolbar_icon_rejected', code=error.code, score=data['score'],
                             runner_up=data['runner_up'], elapsed_ms=data['elapsed_ms'],
                             scale_height=data['scale_height'], match_rect=data['rect'],
                             toolbar_rect=toolbar['rect'], min_score=MIN_SCORE,
                             min_margin=MIN_MARGIN, matcher_revision=data['revision'])
            raise
        probe.record(log, 'toolbar_icon_target', target=TARGET, template_id=18, container_hwnd=toolbar['hwnd'],
                     rect=target['icon_rect'], button_rect=target['button_rect'], score=data['score'],
                     runner_up=data['runner_up'], elapsed_ms=data['elapsed_ms'],
                     scale_height=data['scale_height'], matcher_revision=data['revision'],
                     image_saved=False, network_used=False)
        return target


def click_target(api, root, target, log, cancel_file=None):
    """Re-recognize immediately, hit-test the live toolbar, then send one click."""
    def verify():
        if cancel_file is not None and Path(cancel_file).exists():
            fail('TASK_CANCELLED', '任务已停止，未点击。')
        live = api.metadata(target['hwnd'])
        if (live['pid'] != root['pid'] or not live['visible'] or live['rect'] != target['rect']
                or live['class_name'] != target['class_name'] or not api.user32.IsWindowEnabled(target['hwnd'])):
            fail('ICON_CONTAINER_CHANGED', '工具栏身份、位置或可用状态变化，未点击。')
    verify()
    second = capture_target(root, target, log, cancel_file)
    if second['icon_rect'] != target['icon_rect'] or second['button_rect'] != target['button_rect']:
        fail('ICON_TARGET_CHANGED', '两次识别的菜单位置不一致，未点击。')
    api.health(root['hwnd'], log, 'before_toolbar_icon_click')
    u = api.user32
    box = target['icon_rect']
    point = probe.wintypes.POINT((box[0]+box[2])//2, (box[1]+box[3])//2)
    verify()
    if (int(u.GetForegroundWindow() or 0) != root['hwnd']
            or int(u.WindowFromPoint(point) or 0) != target['hwnd']):
        fail('ICON_TARGET_OBSCURED', '目标未处于前台工具栏或被遮挡，未点击。')
    if any(u.GetAsyncKeyState(k)&0x8000 for k in (1, 2, 4, 16, 17, 18)):
        fail('ICON_INPUT_BUSY', '鼠标按钮或修饰键按住，未点击。')
    if not u.SetCursorPos(point.x, point.y):
        fail('ICON_CURSOR_FAILED', '无法将鼠标移动到已识别菜单，未点击。')
    time.sleep(0.1)
    actual = probe.wintypes.POINT()
    verify()
    if (not u.GetCursorPos(ctypes.byref(actual)) or (actual.x, actual.y) != (point.x, point.y)
            or int(u.GetForegroundWindow() or 0) != root['hwnd']
            or int(u.WindowFromPoint(actual) or 0) != target['hwnd']
            or any(u.GetAsyncKeyState(k)&0x8000 for k in (1, 2, 4, 16, 17, 18))):
        fail('ICON_TARGET_CHANGED', '输入前目标、鼠标或窗口状态变化，未点击。')
    inputs = (probe.NativeInput*2)()
    inputs[0].mi.dwFlags = 0x0002
    inputs[1].mi.dwFlags = 0x0004
    sent = u.SendInput(2, inputs, ctypes.sizeof(probe.NativeInput))
    probe.record(log, 'toolbar_icon_click_sent', requested=2, sent=sent, automatic_retries=False)
    if sent != 2:
        if sent == 1:
            release = probe.NativeInput()
            release.mi.dwFlags = 0x0004
            u.SendInput(1, ctypes.byref(release), ctypes.sizeof(probe.NativeInput))
        fail('ICON_INPUT_FAILED', '鼠标输入不完整，停止且不重试点击。')
