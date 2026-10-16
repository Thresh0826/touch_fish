"""Guarded local WDMS access and calibrated RDP search/file-clipboard adapter."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import re
import time

from adapters.native_windows import NativeInput, NativeWindows
from errors import WDMSError
from moyu_tools.model import fail, zip_name
from moyu_tools.direct_copy import destination


def text_value(result):
    return (result.get("value") or result.get("name") or "").strip()


def row_count(value):
    match = re.fullmatch(r"(?:总行数\s*[:：]?\s*)?([0-9]+)\s*(?:行)?", value.strip())
    if not match:
        fail("MOYU_COUNT_INVALID", "请把行数定位点放在送货单一览的数字上，不能读取其他合计。")
    return int(match.group(1))


def confirm_column(header, cell):
    for item, roles in ((header, (25, 41, 42)), (cell, (29, 41, 42))):
        rect = item.get("rect")
        if (item.get("role") not in roles or not isinstance(rect, list) or len(rect) != 4
                or any(type(n) is not int for n in rect) or rect[0] >= rect[2] or rect[1] >= rect[3]):
            fail("MOYU_COLUMN_UNCONFIRMED", "控件没有完整的表头/单元格范围，未读取其他数字当作单号。")
    h, c = header["rect"], cell["rect"]
    overlap = max(0, min(h[2], c[2])-max(h[0], c[0]))
    if (h[3] > c[1]+2 or overlap < .8*min(h[2]-h[0], c[2]-c[0])
            or not h[0]-2 <= (c[0]+c[2])/2 <= h[2]+2):
        fail("MOYU_COLUMN_UNCONFIRMED", "表头和单号位置不属于同一列，未搜索。")


def remote_result(regions, expected):
    if not isinstance(regions, list) or len(regions) != 2:
        fail("MOYU_REMOTE_UNREADABLE", "远程结果读取不完整。")
    count_text = re.sub(r"\s", "", regions[1].get("text", ""))
    if not re.fullmatch(r"[0-9]+", count_text):
        return None
    count = int(count_text)
    if count != expected:
        return None
    lines = [line for line in regions[0].get("lines", [])
             if line.get("text", "").strip().lower().endswith(".zip")]
    if expected == 0:
        return {} if not lines else None
    if len(lines) != 1:
        return None
    name = zip_name(lines[0]["text"].strip())
    rect = lines[0].get("rect")
    if (not isinstance(rect, list) or len(rect) != 4 or any(type(x) is not int for x in rect)
            or not 0 <= rect[0] < rect[2] or not 0 <= rect[1] < rect[3]):
        fail("MOYU_REMOTE_UNREADABLE", "远程ZIP名称位置不完整，未选择。")
    return {"name": name, "rect": rect}


def single_zip_result(regions):
    if not isinstance(regions, list) or len(regions) != 1:
        fail("MOYU_REMOTE_UNREADABLE", "远程结果区域未完整读取。")
    if not regions[0].get("text", "").strip() and not regions[0].get("lines"):
        return {}
    return remote_result([regions[0], {"text": "1"}], 1)


class Driver:
    def __init__(self, bridge, log):
        self.bridge, self.log = bridge, log
        self.api = NativeWindows()
        self.u = self.api.user32
        self.u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.u.GetAncestor.restype = wintypes.HWND
        self.u.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.u.GetDpiForWindow.restype = wintypes.UINT
        self.u.IsHungAppWindow.argtypes = [wintypes.HWND]
        self.u.IsHungAppWindow.restype = wintypes.BOOL
        self.input_guard = None

    def point(self):
        point = wintypes.POINT()
        if not self.u.GetCursorPos(ctypes.byref(point)):
            fail("MOYU_CURSOR_UNAVAILABLE", "无法读取标记位置。")
        return self.bridge.call("point", {"x": point.x, "y": point.y})

    def identity(self, root, context=None):
        if context:
            context.check()
        current = self.api.metadata(root["hwnd"])
        if (any(current[k] != root[k] for k in ("pid", "class_name", "rect"))
                or self.u.GetDpiForWindow(root["hwnd"]) != root["dpi"]):
            fail("MOYU_LAYOUT_CHANGED", "窗口、位置或大小已变化，请恢复窗口位置或重新定位。")
        return current

    def guard(self, root, context=None, *, foreground=True):
        current = self.identity(root, context)
        if not current["visible"] or current["minimized"]:
            fail("MOYU_LAYOUT_CHANGED", "目标窗口不可见或已最小化，请先恢复窗口。")
        if foreground:
            fg = int(self.u.GetForegroundWindow() or 0)
            if fg != root["hwnd"]:
                def _ancestor(mode):
                    try:
                        return int(self.u.GetAncestor(fg, mode) or 0)
                    except (TypeError, ValueError):
                        return 0
                if _ancestor(2) != root["hwnd"] and _ancestor(3) != root["hwnd"]:
                    fail("MOYU_FOREGROUND_CHANGED", "目标窗口离开前台，已停止输入。")

    def focus(self, root, context):
        self.guard(root, context, foreground=False)
        self.u.SetForegroundWindow(root["hwnd"])
        time.sleep(min(0.15, context.remaining()))
        self.guard(root, context)

    def control(self, target, root, context):
        self.guard(root, context)
        current = self.api.metadata(target["hwnd"])
        if (current["pid"] != root["pid"] or current["class_name"] != target["class_name"]
                or current["rect"] != target["rect"] or not current["visible"]):
            fail("MOYU_CONTROL_CHANGED", "定位控件已变化，未读取或输入。")

    def emit_input(self, root, items, context, point=None):
        if self.input_guard is not None:
            self.input_guard(root, context)
        self.guard(root, context)
        if any(self.u.GetAsyncKeyState(k) & 0x8000 for k in (1, 2, 4, 16, 17, 18, 91, 92)):
            fail("MOYU_INPUT_BUSY", "检测到用户按住键鼠，已停止输入。")
        if point is not None:
            x, y = point
            if not root["rect"][0] <= x < root["rect"][2] or not root["rect"][1] <= y < root["rect"][3]:
                fail("MOYU_POINT_INVALID", "标记位置越出目标窗口。")
            self.u.SetCursorPos(x, y)
            actual = wintypes.POINT()
            if (not self.u.GetCursorPos(ctypes.byref(actual)) or (actual.x, actual.y) != (x, y)
                    or int(self.u.GetAncestor(self.u.WindowFromPoint(actual), 2) or 0) != root["hwnd"]):
                fail("MOYU_TARGET_OBSCURED", "目标位置被遮挡或鼠标落点变化，未输入。")
        self.guard(root, context)
        values = (NativeInput * len(items))(*items)
        sent = self.u.SendInput(len(values), values, ctypes.sizeof(NativeInput))
        if sent != len(values):
            # Release only our submitted downs; never replay an ambiguous action.
            releases = []
            for item in items[:sent]:
                released = NativeInput()
                if item.type == 1 and not item.ki.dwFlags & 2:
                    released.type = 1
                    released.ki.wVk, released.ki.wScan = item.ki.wVk, item.ki.wScan
                    released.ki.dwFlags = item.ki.dwFlags | 2
                    releases.append(released)
                elif item.type == 0 and item.mi.dwFlags & 2:
                    released.mi.dwFlags = 4
                    releases.append(released)
            if releases:
                array = (NativeInput * len(releases))(*releases)
                self.u.SendInput(len(array), array, ctypes.sizeof(NativeInput))
            fail("MOYU_INPUT_INCOMPLETE", "输入未完整发送，已释放程序按键；不会自动重发。")

    def click(self, root, point, context):
        down, up = NativeInput(), NativeInput()
        down.mi.dwFlags, up.mi.dwFlags = 2, 4
        self.emit_input(root, [down, up], context, point)

    def chord(self, root, key, context, modifier=0x11):
        sequence = [(modifier, 0), (key, 0), (key, 2), (modifier, 2)] if modifier else [(key, 0), (key, 2)]
        items = []
        for code, flags in sequence:
            item = NativeInput()
            item.type, item.ki.wVk, item.ki.dwFlags = 1, code, flags
            items.append(item)
        self.emit_input(root, items, context)

    def type_text(self, root, text, context):
        items = []
        encoded = text.encode("utf-16-le")
        for i in range(0, len(encoded), 2):
            code = int.from_bytes(encoded[i:i+2], "little")
            for flags in (4, 6):
                item = NativeInput()
                item.type, item.ki.wScan, item.ki.dwFlags = 1, code, flags
                items.append(item)
        self.emit_input(root, items, context)


class LocalQuery:
    def __init__(self, driver, profile):
        self.d, self.p = driver, profile["wdms"]
        self.root = self.p["root"]
        self.current_segment = None

    def ghost(self, hwnd):
        if not hwnd:
            return False
        other = self.d.api.metadata(hwnd)
        return other["class_name"] == "Ghost" and other["rect"] == self.root["rect"]

    def attach(self, context):
        while True:
            current = self.d.identity(self.root, context)
            if current["minimized"]:
                fail("MOYU_LAYOUT_CHANGED", "WDMS已最小化，请先恢复窗口。")
            if current["visible"] and not self.d.u.IsHungAppWindow(self.root["hwnd"]):
                try:
                    self.d.api.health(self.root["hwnd"], self.d.log, "moyu_attach")
                except WDMSError as error:
                    if error.code not in ("WDMS_UNRESPONSIVE", "WDMS_HEALTH_CHECK_FAILED"):
                        raise
                else:
                    if self.ghost(int(self.d.u.GetForegroundWindow() or 0)):
                        time.sleep(min(.1, context.remaining()))
                        continue
                    break
            time.sleep(min(.1, context.remaining()))
        self.d.focus(self.root, context)
        target = self.p["query"]
        self.d.control(target, self.root, context)
        left, top, right, bottom = target["rect"]
        self.d.u.SetCursorPos((left+right)//2, (top+bottom)//2)

    def date(self, key, context, value=None):
        target = self.p[key]
        self.d.control(target, self.root, context)
        payload = {"root": self.root, "hwnd": target["hwnd"]}
        if value is not None:
            payload["value"] = value
        result = self.d.bridge.call("date", payload, context)["value"]
        self.d.control(target, self.root, context)
        return result

    def set_dates(self, segment, context):
        while True:
            signal = self.signal(None, context)
            if not signal["busy"] and signal["responsive"]:
                break
            time.sleep(min(0.1, context.remaining()))
        self.guard_conditions(context)
        if hasattr(segment, "contract"):
            self.contract_field(context)
        self.date("start", context, segment.start.isoformat())
        self.date("end", context, segment.end.isoformat())
        if hasattr(segment, "contract"):
            self.contract(context, segment.contract)
        self.confirm(segment, context)

    def condition_payload(self):
        excluded = [self.p["start"]["hwnd"], self.p["end"]["hwnd"]]
        if "contract" in self.p:
            excluded.append(self.p["contract"]["hwnd"])
        return {"root": self.root, "rect": self.p["conditions"], "dates": excluded}

    def guard_conditions(self, context):
        value = self.d.bridge.call("conditions", self.condition_payload(), context)["hash"]
        if value != self.p["conditions_hash"]:
            fail("MOYU_CONDITIONS_CHANGED", "货主等固定筛选条件或布局已变化，未混用旧进度；请恢复原条件或重新定位并新建任务。")

    def contract_field(self, context):
        if "contract" not in self.p or "contract_label" not in self.p:
            fail("MOYU_CONTRACT_UNBOUND", "旧定位未绑定合同编号，请按新版向导重新定位并新建表格任务。")
        if re.sub(r"\s", "", self.read("contract_label", context)).rstrip(":：") != "合同编号":
            fail("MOYU_CONTRACT_LABEL", "输入框旁不是“合同编号”，未填写合同号、管理编号或其他字段。")
        target = self.p["contract"]
        self.d.control(target, self.root, context)
        if not re.fullmatch(r"(?:.*\.)?EDIT(?:\..*)?", target["class_name"], re.IGNORECASE):
            fail("MOYU_CONTRACT_CONTROL", "合同编号定位不是可确认的原生文本框，未输入。")

    def contract(self, context, value=None):
        self.contract_field(context)
        payload = {"root": self.root, "hwnd": self.p["contract"]["hwnd"]}
        if value is not None:
            payload["value"] = value
        result = self.d.bridge.call("edit", payload, context)["value"]
        self.d.control(self.p["contract"], self.root, context)
        return result

    def confirm(self, segment, context):
        if (self.date("start", context) != segment.start.isoformat()
                or self.date("end", context) != segment.end.isoformat()):
            fail("MOYU_DATE_MISMATCH", "WDMS日期回读与本段不一致，未继续。")
        if hasattr(segment, "contract") and self.contract(context) != segment.contract:
            fail("MOYU_CONTRACT_MISMATCH", "合同编号回读与当前表格行不一致，未查询或搜索。")
        self.current_segment = segment

    def click(self, context):
        self.d.api.query_input_guard = lambda: self.guard_query_input(context)
        try:
            self.d.api.click_query_input(self.root, self.p["query"]["hwnd"], self.d.log)
        finally:
            self.d.api.query_input_guard = None

    def guard_query_input(self, context):
        self.query_button(context)
        if self.current_segment is not None:
            self.confirm(self.current_segment, context)
            self.guard_conditions(context)
        self.d.guard(self.root, context)

    def query_button(self, context):
        caption = re.sub(r"\s", "", self.read("query", context)).replace("&", "")
        if not re.fullmatch(r"查询(?:[（(][A-Za-z][）)])?", caption):
            fail("MOYU_QUERY_BUTTON_UNCONFIRMED", "标记按钮不是可确认的“查询”，未点击导出或其他按钮。")

    def signal(self, segment, context):
        current = self.d.identity(self.root, context)
        if current["minimized"]:
            fail("MOYU_LAYOUT_CHANGED", "WDMS已最小化，未读取缓存或发送输入。")
        if not current["visible"] or self.d.u.IsHungAppWindow(self.root["hwnd"]):
            # Windows may substitute a Ghost window while the UI thread is hung.
            # Never read its cached pixels or count its global cursor as our busy evidence.
            return {"busy": False, "responsive": False}
        foreground = int(self.d.u.GetForegroundWindow() or 0)
        if foreground != self.root["hwnd"]:
            if self.ghost(foreground):
                return {"busy": False, "responsive": False}
            fail("MOYU_FOREGROUND_CHANGED", "WDMS离开前台，停止且未读取其他窗口。")
        self.d.guard(self.root, context)
        target = self.p["query"]
        self.d.control(target, self.root, context)
        actual = wintypes.POINT()
        if not self.d.u.GetCursorPos(ctypes.byref(actual)):
            fail("MOYU_CURSOR_CHANGED", "无法读取鼠标位置，未使用缓存判断。")
        hit = int(self.d.u.WindowFromPoint(actual) or 0)
        if hit != target["hwnd"]:
            if hit and self.ghost(int(self.d.u.GetAncestor(hit, 2) or 0)):
                return {"busy": False, "responsive": False}
            fail("MOYU_CURSOR_CHANGED", "鼠标已离开查询按钮，不能用光标确认本段完成。")
        cursor = self.d.api.cursor_state()
        if not cursor.get("cursor_visible"):
            return {"busy": False, "responsive": False}
        if cursor["standard_wait_cursor"]:
            return {"busy": True, "responsive": False}
        try:
            self.d.api.health(self.root["hwnd"], self.d.log, "moyu_query_wait")
        except WDMSError as error:
            if error.code in ("WDMS_UNRESPONSIVE", "WDMS_HEALTH_CHECK_FAILED"):
                return {"busy": False, "responsive": False}
            raise
        return {"busy": False, "responsive": True}

    def record(self, key, context):
        target = self.p[key]
        self.d.control(target, self.root, context)
        return self.d.bridge.call("read", {"root": self.root, "point": target["point"]}, context)

    def read(self, key, context):
        return text_value(self.record(key, context))

    def count(self, context):
        label = re.sub(r"\s", "", self.read("count_label", context))
        if not re.fullmatch(r"总行数[:：]?(?:[0-9]+行?)?", label):
            fail("MOYU_COUNT_LABEL_UNCONFIRMED", "行数旁的标签不是“总行数”，未使用整箱数或合同明细数量。")
        return row_count(self.read("count", context))

    def snapshot(self, segment, context):
        count = self.count(context)
        if count != 1:
            return {"count": count, "number": None}
        header = self.record("header", context)
        if text_value(header) != "送货单号":
            if self.p.get("column_mode") == "visible":
                fail("MOYU_COLUMN_HIDDEN", "送货单号列已移位或不可见，请恢复定位时的可见列布局；未滚动或读取其他列。")
            position = self.p.get("scroll", -1)
            if position < 0:
                fail("MOYU_COLUMN_HIDDEN", "送货单号列未显示，请向右滚动到定位时的位置后继续。")
            self.d.bridge.call("scroll", {"root": self.root, "hwnd": self.p["cell"]["hwnd"],
                                           "position": position}, context)
            header = self.record("header", context)
        if text_value(header) != "送货单号":
            fail("MOYU_COLUMN_UNCONFIRMED", "没有确认送货单号表头，未猜测其他数字。")
        cell = self.record("cell", context)
        confirm_column(header, cell)
        return {"count": 1, "number": text_value(cell)}


class RemoteFiles:
    def __init__(self, driver, profile):
        self.d, self.p = driver, profile["remote"]
        self.root = self.p["root"]
        self.d.input_guard = lambda root, context: self.guard(context) if root["hwnd"] == self.root["hwnd"] else None
        self.explorer = None

    def guard(self, context):
        self.d.guard(self.root, context)
        result = self.d.bridge.call("hash", {"root": self.root, "rect": self.p["anchor"]}, context)
        if result["hash"] != self.p["anchor_hash"]:
            fail("MOYU_REMOTE_LAYOUT_CHANGED", "远程软件固定标识已变化或被遮挡，请恢复界面或重新定位。")

    def clear(self, context):
        return self.d.bridge.call("clear", context=context)["sequence"]

    def keyword(self, number, context):
        self.guard(context)
        rect = self.p["search"]
        self.d.click(self.root, ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), context)
        self.d.chord(self.root, 0x41, context)
        self.d.type_text(self.root, number, context)
        baseline = self.clear(context)
        self.guard(context)
        self.d.chord(self.root, 0x41, context)
        self.d.chord(self.root, 0x43, context)
        while True:
            context.check()
            try:
                value = self.d.bridge.call("text", context=context)
            except WDMSError as error:
                if error.code not in ("MOYU_NATIVE_UNAVAILABLE", "MOYU_READ_PENDING"):
                    raise
                time.sleep(min(0.1, context.remaining()))
                continue
            if value["sequence"] != baseline and value["text"] == number:
                break
            time.sleep(min(0.1, context.remaining()))
        self.guard(context)
        if self.p.get("submit_enter", False):
            self.d.chord(self.root, 0x0D, context, modifier=None)

    def read_results(self, context):
        self.guard(context)
        rects = [self.p["results"]] if self.p.get("mode") == "single_zip" else [self.p["results"], self.p["count"]]
        result = self.d.bridge.call("ocr", {"root": self.root, "rects": rects}, context)
        self.guard(context)
        return result["regions"]

    def wait_results(self, count, context):
        stable_since, previous = None, None
        while True:
            context.check()
            try:
                result = remote_result(self.read_results(context), count)
            except WDMSError as error:
                if error.code != "MOYU_READ_PENDING":
                    raise
                result = None
            context.check()
            if result is None:
                stable_since, previous = None, None
            elif result != previous:
                previous, stable_since = result, time.monotonic()
            elif stable_since is not None and time.monotonic()-stable_since >= 2:
                return result
            time.sleep(min(0.2, context.remaining()))

    def search(self, number, context):
        if self.p.get("mode") == "single_zip":
            return self.search_one(number, context)
        self.d.focus(self.root, context)
        # An impossible filename clears stale results. A confirmed zero state
        # must precede the real keyword's confirmed single-ZIP state.
        self.keyword("__moyu_no_match_"+str(time.monotonic_ns())+"__", context)
        self.wait_results(0, context)
        self.keyword(number, context)
        result = self.wait_results(1, context)
        self.guard(context)
        if remote_result(self.read_results(context), 1) != result:
            fail("MOYU_REMOTE_RESULT_CHANGED", "选择前搜索结果已变化，未复制。")
        origin = self.p["results"]
        rect = result["rect"]
        point = (origin[0]+(rect[0]+rect[2])//2, origin[1]+(rect[1]+rect[3])//2)
        if not origin[0] <= point[0] < origin[2] or not origin[1] <= point[1] < origin[3]:
            fail("MOYU_REMOTE_RESULT_CHANGED", "文件名位置越出标定区域，未复制。")
        baseline = self.clear(context)
        self.guard(context)
        self.d.click(self.root, point, context)
        self.d.chord(self.root, 0x43, context)
        while True:
            context.check()
            try:
                selected = self.d.bridge.call("file", context=context)
            except WDMSError as error:
                if error.code not in ("MOYU_NATIVE_UNAVAILABLE", "MOYU_READ_PENDING"):
                    raise
            else:
                if selected["sequence"] != baseline:
                    if zip_name(selected["name"]).casefold() != result["name"].casefold():
                        fail("MOYU_CLIPBOARD_MISMATCH", "剪贴板文件名与本次唯一ZIP结果不符，未粘贴。")
                    return selected
            time.sleep(min(0.2, context.remaining()))

    def read_keyword(self, context):
        rect = self.p["search"]
        self.d.click(self.root, ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), context)
        self.d.chord(self.root, 0x41, context)
        baseline = self.clear(context)
        self.d.chord(self.root, 0x43, context)
        started = time.monotonic()
        while True:
            context.check()
            try:
                result = self.d.bridge.call("text", context=context)
                if result["sequence"] != baseline and isinstance(result["text"], str):
                    return result["text"]
                if result.get("text") == "" and time.monotonic()-started >= 1:
                    return ""
            except WDMSError as error:
                if error.code not in ("MOYU_READ_PENDING", "MOYU_NATIVE_UNAVAILABLE"):
                    raise
            time.sleep(min(.1, context.remaining()))

    def stable_one(self, context, *, before=None, same_keyword=True, allow_many=False):
        previous, since = None, None
        while True:
            context.check()
            try:
                regions = self.read_results(context)
                current = single_zip_result(regions)
                if current is None and allow_many:
                    current = {"ambiguous": True, "signature": regions[0].get("text", "")}
            except WDMSError as error:
                if error.code != "MOYU_READ_PENDING":
                    raise
                current = None
            acceptable = current is not None and (same_keyword or current and current != before)
            if not acceptable:
                previous, since = None, None
            elif current != previous or since is None:
                previous, since = current, time.monotonic()
            elif time.monotonic()-since >= 2:
                return current
            time.sleep(min(.2, context.remaining()))

    def search_one(self, number, context):
        self.d.focus(self.root, context)
        previous_keyword = self.read_keyword(context)
        before = self.stable_one(context, allow_many=True)
        self.keyword(number, context)  # Exactly one search term, no dummy searches.
        result = self.stable_one(context, before=before, same_keyword=previous_keyword == number)
        if not result:
            fail("MOYU_REMOTE_NOT_FOUND", "本次搜索没有找到ZIP，已暂停。")
        if single_zip_result(self.read_results(context)) != result:
            fail("MOYU_REMOTE_RESULT_CHANGED", "搜索结果已变化，未复制。")
        origin, rect = self.p["results"], result["rect"]
        point = (origin[0]+(rect[0]+rect[2])//2, origin[1]+(rect[1]+rect[3])//2)
        if not origin[0] <= point[0] < origin[2] or not origin[1] <= point[1] < origin[3]:
            fail("MOYU_REMOTE_RESULT_CHANGED", "结果文件位置越出所选区域。")
        baseline = self.clear(context)
        self.d.click(self.root, point, context)
        self.d.chord(self.root, 0x41, context)  # Select all results to prove clipboard uniqueness.
        self.d.chord(self.root, 0x43, context)
        while True:
            context.check()
            try:
                selected = self.d.bridge.call("file", context=context)
                if selected["sequence"] != baseline:
                    if zip_name(selected["name"]).casefold() != result["name"].casefold():
                        fail("MOYU_CLIPBOARD_MISMATCH", "本次复制的实际文件与结果不一致，未粘贴。")
                    return selected
            except WDMSError as error:
                if error.code not in ("MOYU_READ_PENDING", "MOYU_NATIVE_UNAVAILABLE"):
                    raise
            time.sleep(min(.2, context.remaining()))

    def paste(self, folder, selected, context):
        # Explorer performs the standard redirected file paste; no remote drive,
        # business folder scan or direct server connection is introduced.
        if self.explorer is None:
            existing = (self.d.bridge.call("folder", {"path": str(folder)}, context)["windows"]
                        if self.p.get("mode") == "single_zip" else [])
            if not existing or len(existing) == 1 and existing[0].get("minimized", False):
                self.d.bridge.call("open_folder", {"path": str(folder)}, context)
        else:
            self.d.bridge.call("navigate_folder", {"hwnd": self.explorer["hwnd"],
                                                     "previous": self.explorer["path"], "path": str(folder)}, context)
        while True:
            context.check()
            windows = self.d.bridge.call("folder", {"path": str(folder)}, context)["windows"]
            if len(windows) == 1:
                root = windows[0]
                if root["class_name"] not in ("CabinetWClass", "ExploreWClass"):
                    fail("MOYU_FOLDER_UNCONFIRMED", "本地目标窗口不是已确认的资源管理器，未粘贴。")
                break
            if len(windows) > 1:
                fail("MOYU_FOLDER_AMBIGUOUS", "同一目标目录有多个资源管理器窗口，请保留一个后继续。")
            time.sleep(min(0.2, context.remaining()))
        self.d.focus(root, context)
        self.explorer = {"hwnd": root["hwnd"], "path": str(folder)}
        current = self.d.bridge.call("file", context=context)
        if current != selected:
            fail("MOYU_CLIPBOARD_CHANGED", "切换本地窗口后文件剪贴板已变化，未粘贴。")
        if self.p.get("mode") == "single_zip":
            target = destination(folder, selected["name"])
            if target.exists() or target.is_symlink():
                fail("MOYU_NAME_CONFLICT", "粘贴前发现目标已有同名文件，已暂停且未覆盖。")
        self.d.chord(root, 0x56, context)
