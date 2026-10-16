"""Native Windows boundary: window identity, allowlisted text and guarded input."""
from __future__ import annotations
import ctypes
import re
import time
from ctypes import wintypes
from pathlib import Path
from errors import NativeTextReadError, WDMSError
from core.query_policy import DEFAULT_QUERY_POLICY, NativeMessagePolicy
from services.event_log import record, error_info


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]



class KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]



class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]



class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]



class NativeInput(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", wintypes.DWORD), ("payload", InputUnion)]



class CursorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]



def text_read_allowed(data: dict) -> bool:
    return bool(data.get("visible")) and re.match(r"^WindowsForms10\.(STATIC|BUTTON|COMBOBOX)\.", data.get("class_name", "")) is not None



class NativeWindows:
    """Only OS window metadata plus a 500ms WM_NULL responsiveness check."""

    message_policy = DEFAULT_QUERY_POLICY.messages

    def __init__(self, message_policy: NativeMessagePolicy = DEFAULT_QUERY_POLICY.messages) -> None:
        self.message_policy = message_policy
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        # Keep GetWindowRect, cursor placement and hit testing in the same pixel space.
        self.user32.SetProcessDPIAware()
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        definitions = {
            "GetCursorInfo": ([ctypes.POINTER(CursorInfo)], wintypes.BOOL),
            "LoadCursorW": ([wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HANDLE),
            "GetForegroundWindow": ([], wintypes.HWND),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "SetCursorPos": ([ctypes.c_int, ctypes.c_int], wintypes.BOOL),
            "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            "SendInput": ([wintypes.UINT, ctypes.POINTER(NativeInput), ctypes.c_int], wintypes.UINT),
            "EnumWindows": ([self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetParent": ([wintypes.HWND], wintypes.HWND),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetDlgCtrlID": ([wintypes.HWND], ctypes.c_int),
            "GetWindowLongW": ([wintypes.HWND, ctypes.c_int], wintypes.LONG),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowEnabled": ([wintypes.HWND], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], wintypes.BOOL),
            "IsHungAppWindow": ([wintypes.HWND], wintypes.BOOL),
            "SendMessageTimeoutW": ([wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result

    def cursor_state(self) -> dict:
        info = CursorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetCursorInfo(ctypes.byref(info)):
            raise WDMSError("CURSOR_OBSERVATION_FAILED", "无法取得鼠标光标状态。")
        waiting = int(self.user32.LoadCursorW(None, ctypes.c_void_p(32514)) or 0)
        starting = int(self.user32.LoadCursorW(None, ctypes.c_void_p(32650)) or 0)
        current = int(info.hCursor or 0)
        return {"cursor_handle": current, "cursor_visible": bool(info.flags & 1),
                "standard_wait_cursor": current != 0 and current in (waiting, starting)}

    def _guard_query_input(self, main, button, point=None):
        """The last boundary before SendInput: no logging, sleeping or retries."""
        guard = getattr(self, "query_input_guard", None)
        if guard is not None:
            # Owner reads/cancellation may perform I/O. Finish them before the
            # final native checks below, rather than between checks and input.
            guard()
        u = self.user32
        root = self.metadata(main["hwnd"])
        if (root["pid"] != main["pid"] or not root["visible"] or not u.IsWindowEnabled(main["hwnd"])
                or main.get("class_name") is not None and root["class_name"] != main["class_name"]):
            raise WDMSError("QUERY_TARGET_CHANGED", "输入前主窗口身份变化，未查询。")
        live = self.metadata(button)
        if live["pid"] != main["pid"] or not live["visible"] or not u.IsWindowEnabled(button):
            raise WDMSError("QUERY_TARGET_CHANGED", "输入前查询按钮身份或可用状态变化，未查询。")
        actual = wintypes.POINT()
        if not u.GetCursorPos(ctypes.byref(actual)):
            raise WDMSError("QUERY_CURSOR_FAILED", "无法核对输入前的指针位置，未查询。")
        if ((point is not None and (actual.x, actual.y) != (point.x, point.y))
                or int(u.WindowFromPoint(actual) or 0) != button):
            raise WDMSError("QUERY_TARGET_CHANGED", "输入前鼠标位置或按钮命中变化，未查询。")
        if guard is not None:
            cursor = self.cursor_state()
            if not cursor.get("cursor_visible") or cursor["standard_wait_cursor"]:
                raise WDMSError("QUERY_ALREADY_RUNNING", "输入前WDMS空闲状态已变化，未发送查询。")
        if any(u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 0x5B, 0x5C, 0x46)):
            raise WDMSError("QUERY_INPUT_BUSY", "输入前检测到按键或鼠标按钮按住，未查询。")
        if int(u.GetForegroundWindow() or 0) != main["hwnd"]:
            raise WDMSError("QUERY_FOREGROUND_CHANGED", "日志记录后WDMS已离开前台，未发送查询。")

    def query_shortcut_input(self, main: dict, button: int, log: Path) -> None:
        """One Alt+F sequence to the verified foreground WDMS window."""
        u = self.user32
        u.SetForegroundWindow(main["hwnd"])
        deadline = time.monotonic() + 1.0
        while int(u.GetForegroundWindow() or 0) != main["hwnd"]:
            if time.monotonic() >= deadline:
                raise WDMSError("QUERY_FOREGROUND_FAILED", "WDMS 主窗口未处于前台，未发送 Alt+F。请关闭弹窗并手动激活 WDMS。")
            time.sleep(0.02)
        live = self.metadata(button)
        if live["pid"] != main["pid"] or not live["visible"] or not u.IsWindowEnabled(button):
            raise WDMSError("QUERY_TARGET_CHANGED", "快捷键发送前查询按钮状态变化，未发送 Alt+F。")
        if any(u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 0x5B, 0x5C, 0x46)):
            raise WDMSError("QUERY_INPUT_BUSY", "检测到鼠标按钮、修饰键或 F 键按住，未发送 Alt+F。")
        if int(u.GetForegroundWindow() or 0) != main["hwnd"]:
            raise WDMSError("QUERY_FOREGROUND_FAILED", "快捷键发送前前台窗口变化，未发送 Alt+F。")
        inputs = (NativeInput * 4)()
        for item, (key, flags) in zip(inputs, ((18, 0), (0x46, 0), (0x46, 2), (18, 2))):
            item.type = 1
            item.ki.wVk = key
            item.ki.dwFlags = flags
        record(log, "before_keyboard_input", keys="Alt+F", main_hwnd=main["hwnd"])
        self._guard_query_input(main, button)
        sent = u.SendInput(4, inputs, ctypes.sizeof(NativeInput))
        if sent != 4:
            if sent:
                # Release keys only: do not repeat the shortcut/query.
                releases = (NativeInput * 2)()
                for item, key in zip(releases, (0x46, 18)):
                    item.type = 1
                    item.ki.wVk = key
                    item.ki.dwFlags = 2
                u.SendInput(2, releases, ctypes.sizeof(NativeInput))
            record(log, "keyboard_input_sent", keys="Alt+F", requested=4, sent=sent)
            raise WDMSError("QUERY_INPUT_FAILED", "Alt+F 输入未完整发送，停止测试且不重试查询。")
        record(log, "keyboard_input_sent", keys="Alt+F", requested=4, sent=sent)

    def click_query_input(self, main: dict, button: int, log: Path) -> None:
        """One OS mouse click on the live, unobstructed button; no message fallback."""
        u = self.user32
        u.SetForegroundWindow(main["hwnd"])
        deadline = time.monotonic() + 1.0
        while int(u.GetForegroundWindow() or 0) != main["hwnd"]:
            if time.monotonic() >= deadline:
                raise WDMSError("QUERY_FOREGROUND_FAILED", "无法将 WDMS 主窗口置于前台，请关闭弹窗并手动激活 WDMS 后测试。未点击。")
            time.sleep(0.02)
        live = self.metadata(button)
        if live["pid"] != main["pid"] or not live["visible"] or not u.IsWindowEnabled(button):
            raise WDMSError("QUERY_TARGET_CHANGED", "点击前查询按钮状态变化，未点击。")
        left, top, right, bottom = live["rect"]
        if right <= left or bottom <= top:
            raise WDMSError("QUERY_RECT_INVALID", "查询按钮尺寸无效，未点击。")
        point = wintypes.POINT((left + right) // 2, (top + bottom) // 2)
        if int(u.WindowFromPoint(point) or 0) != button:
            raise WDMSError("QUERY_BUTTON_OBSCURED", "查询按钮被遮挡或命中位置不属于按钮，未点击。")
        if any(u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18)):
            raise WDMSError("QUERY_INPUT_BUSY", "检测到鼠标按钮或修饰键按住，未点击。")
        if not u.SetCursorPos(point.x, point.y):
            raise WDMSError("QUERY_CURSOR_FAILED", "无法将鼠标移至查询按钮，未点击。")
        actual = wintypes.POINT()
        if (not u.GetCursorPos(ctypes.byref(actual)) or actual.x != point.x or actual.y != point.y
                or int(u.GetForegroundWindow() or 0) != main["hwnd"]
                or int(u.WindowFromPoint(actual) or 0) != button):
            raise WDMSError("QUERY_TARGET_CHANGED", "输入前鼠标位置、前台窗口或按钮命中变化，未点击。")
        record(log, "before_mouse_input", search_hwnd=button, x=point.x, y=point.y)
        # Queue down/up together so the mouse is not held while WDMS is busy.
        inputs = (NativeInput * 2)()
        inputs[0].mi.dwFlags = 0x0002
        inputs[1].mi.dwFlags = 0x0004
        self._guard_query_input(main, button, point)
        sent = u.SendInput(2, inputs, ctypes.sizeof(NativeInput))
        if sent != 2:
            if sent == 1:
                # Release only; never issue another down/click.
                release = NativeInput()
                release.mi.dwFlags = 0x0004
                u.SendInput(1, ctypes.byref(release), ctypes.sizeof(NativeInput))
            record(log, "mouse_input_sent", requested=2, sent=sent)
            raise WDMSError("QUERY_INPUT_FAILED", "鼠标输入未完整发送，停止测试且不重试查询。")
        record(log, "mouse_input_sent", requested=2, sent=sent)

    def title(self, hwnd: int) -> str:
        # Cross-process GetWindowText reads the window caption, not WM_GETTEXT
        # from arbitrary controls. Never use it for edit/password fields.
        buffer = ctypes.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def metadata(self, hwnd: int, include_title: bool = False) -> dict:
        pid = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        name = ctypes.create_unicode_buffer(256)
        if not self.user32.GetClassNameW(hwnd, name, len(name)):
            raise ctypes.WinError(ctypes.get_last_error())
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        data = {"hwnd": hwnd, "pid": pid.value, "class_name": name.value,
                "owner_hwnd": int(self.user32.GetWindow(hwnd, 4) or 0),
                "visible": bool(self.user32.IsWindowVisible(hwnd)),
                "minimized": bool(self.user32.IsIconic(hwnd)),
                "rect": [rect.left, rect.top, rect.right, rect.bottom]}
        if include_title:
            data["title"] = self.title(hwnd)
        else:
            # Native numeric IDs are NOT UIA AutomationIds.
            data["native_control_id"] = self.user32.GetDlgCtrlID(hwnd)
        return data

    def find_main(self, title: str, log: Path) -> list:
        found, failures = [], []
        visited = 0

        def callback(hwnd, unused):
            nonlocal visited
            visited += 1
            if visited > 1024:
                failures.append(WDMSError("WINDOW_ENUM_LIMIT", "顶层窗口枚举超过 1024 个，结果不完整。"))
                return False
            try:
                if self.user32.IsWindowVisible(hwnd) and not self.user32.GetWindow(hwnd, 4) and self.title(hwnd) == title:
                    found.append(self.metadata(int(hwnd), include_title=True))
            except Exception as error:
                failures.append(error)
                return False
            return True

        record(log, "before_window_enumeration")
        function = self.callback_type(callback)
        ok = self.user32.EnumWindows(function, 0)
        if failures:
            raise failures[0]
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        record(log, "after_window_enumeration", matches=found)
        return found

    def health(self, hwnd: int, log: Path, stage: str) -> None:
        record(log, "before_health_check", stage=stage, hwnd=hwnd)
        if self.user32.IsHungAppWindow(hwnd):
            raise WDMSError("WDMS_UNRESPONSIVE", "WDMS 已被标记为未响应，诊断停止。")
        result = ctypes.c_size_t()
        ctypes.set_last_error(0)
        # SMTO_BLOCK | SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT. No input or query.
        ok = self.user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x23, message_policy(self).health_ms, ctypes.byref(result))
        if not ok:
            code = ctypes.get_last_error()
            if code == 5:
                raise WDMSError("WDMS_ACCESS_DENIED", "WM_NULL 探活被拒绝访问，检查运行权限。")
            raise WDMSError("WDMS_HEALTH_CHECK_FAILED", f"{message_policy(self).health_ms}ms 探活未成功，WinError={code}；诊断停止。")
        record(log, "after_health_check", stage=stage, responsive=True)

    def child_handles(self, hwnd: int):
        child = self.user32.GetWindow(hwnd, 5)  # GW_CHILD: immediate children.
        seen = set()
        while child:
            child = int(child)
            if child in seen:
                raise WDMSError("WINDOW_TREE_CHANGED", "窗口列表出现循环，停止枚举。")
            seen.add(child)
            yield child
            child = self.user32.GetWindow(child, 2)  # GW_HWNDNEXT

    def read_label_text(self, data: dict, log: Path) -> str:
        if not text_read_allowed(data):
            raise WDMSError("TEXT_CONTROL_NOT_ALLOWED", "只允许读取可见 WinForms 标签、按钮和下拉框；不读输入框或数据表。")
        hwnd = data["hwnd"]
        record(log, "before_native_text", hwnd=hwnd, class_name=data["class_name"])
        buffer = ctypes.create_unicode_buffer(1024)
        result = ctypes.c_size_t()
        ctypes.set_last_error(0)
        # WM_GETTEXT (< WM_USER) is marshalled by Windows across processes.
        # Read-only messages use the shared policy and parent watchdog.
        ok = self.user32.SendMessageTimeoutW(hwnd, 0x000D, len(buffer), ctypes.cast(buffer, ctypes.c_void_p).value, 0x23, message_policy(self).text_read_ms, ctypes.byref(result))
        if not ok:
            code = ctypes.get_last_error()
            raise NativeTextReadError("NATIVE_TEXT_READ_FAILED",
                f"控件文本读取未成功，WinError={code}；停止本次扫描。",
                winerror=code, hwnd=hwnd, class_name=data["class_name"])
        if result.value >= len(buffer) - 1:
            raise WDMSError("NATIVE_TEXT_TOO_LONG", "控件文本可能被截断，停止读取。")
        return buffer.value

    def read_owner_list(self, data: dict, log: Path) -> list:
        if not data["class_name"].startswith("WindowsForms10.COMBOBOX."):
            raise WDMSError("OWNER_LIST_CONTROL_INVALID", "货主清单读取只允许原生 WinForms 下拉框。")
        hwnd = data["hwnd"]
        style = self.user32.GetWindowLongW(hwnd, -16)
        if style & 0x30 and not style & 0x200:  # owner-drawn without CBS_HASSTRINGS
            raise WDMSError("OWNER_LIST_READ_FAILED", "货主下拉框不使用原生字符串列表，不能按文本列表读取。")

        def send(message, parameter=0, pointer=0):
            record(log, "before_combo_read", hwnd=hwnd, message=message, index=parameter)
            result = ctypes.c_size_t()
            ctypes.set_last_error(0)
            ok = self.user32.SendMessageTimeoutW(hwnd, message, parameter, pointer, 0x23, message_policy(self).combo_read_ms, ctypes.byref(result))
            if not ok:
                raise WDMSError("OWNER_LIST_READ_FAILED", f"货主清单读取未成功，WinError={ctypes.get_last_error()}。")
            value = ctypes.c_ssize_t(result.value).value
            if value < 0:
                raise WDMSError("OWNER_LIST_READ_FAILED", "货主下拉框未返回有效的原生列表数据。")
            return value

        count = send(0x0146)  # CB_GETCOUNT
        if not 1 <= count <= 100:
            raise WDMSError("OWNER_LIST_INVALID", "货主下拉项数量不在 1–100 范围内。")
        names = []
        for index in range(count):
            length = send(0x0149, index)  # CB_GETLBTEXTLEN
            if not 1 <= length <= 512:
                raise WDMSError("OWNER_LIST_READ_FAILED", "货主列表项为空或过长，未继续读取。")
            buffer = ctypes.create_unicode_buffer(length + 1)
            actual = send(0x0148, index, ctypes.cast(buffer, ctypes.c_void_p).value)  # CB_GETLBTEXT
            if actual > length or not buffer.value.strip():
                raise WDMSError("OWNER_LIST_READ_FAILED", "货主列表文本无法完整读取。")
            names.append(buffer.value)
            record(log, "owner_list_item", index=index, name=buffer.value)
        record(log, "owner_list_complete", count=len(names), unique_count=len(set(names)), names=names)
        return names



def owner_combo_from_events(events: list) -> dict:
    texts = [item for item in events if item["event"] == "native_text"]
    labels = [item for item in texts if item["class_name"].startswith("WindowsForms10.STATIC.") and item["text"].strip().rstrip(":：") == "货主"]
    if len(labels) != 1:
        raise WDMSError("OWNER_LABEL_AMBIGUOUS", "未找到唯一的货主标签，未读取任何下拉列表。")
    rect = labels[0]["rect"]
    candidates = [item for item in texts if item["class_name"].startswith("WindowsForms10.COMBOBOX.") and item["rect"][0] >= rect[2] and abs((item["rect"][1] + item["rect"][3]) - (rect[1] + rect[3])) <= 10]
    if not candidates:
        raise WDMSError("OWNER_COMBO_NOT_FOUND", "未找到与货主标签同行的下拉框。")
    nearest = min(item["rect"][0] for item in candidates)
    matches = [item for item in candidates if item["rect"][0] == nearest]
    if len(matches) != 1:
        raise WDMSError("OWNER_COMBO_AMBIGUOUS", "货主标签右侧的最近下拉框不唯一。")
    return matches[0]



def display_number_text(value) -> str:
    """WDMS 数值标签的显示容错：去掉首尾空白与多余的尾部小数点（例如 18585069.）。"""
    text = str(value).strip()
    if text.endswith("."):
        text = text[:-1]
    return text



def read_inventory_values(api, controls: dict, log: Path) -> dict:
    """读取整件数、散件数（必需）与在库数量合计（可选，仅作诊断与刷新判据）。

    在库数量合计不进入统计表：它可能被 WDMS 显示成 18585069. 这类带多余小数点的
    文本，因此只做显示层归一，不要求可解析为数值，也不阻止采集。
    """
    values = {}
    for key in ("whole", "loose"):
        values[key] = display_number_text(api.read_label_text(controls[key], log))
    total = controls.get("total")
    values["total"] = None
    if total is not None:
        try:
            values["total"] = display_number_text(api.read_label_text(total, log))
        except WDMSError as error:
            record(log, "query_diagnostic_unavailable", field="total", **error_info(error))
    return values



def inventory_value_control(texts: list, label: str) -> dict:
    labels = [item for item in texts if item["class_name"].startswith("WindowsForms10.STATIC.") and item["text"].strip().rstrip(":：") == label]
    if len(labels) != 1:
        raise WDMSError("INVENTORY_LABEL_AMBIGUOUS", f"未找到唯一的{label}标签，未触发查询。")
    rect = labels[0]["rect"]
    values = [item for item in texts if item["class_name"].startswith("WindowsForms10.STATIC.") and item["rect"][0] >= rect[2] and abs(item["rect"][1] + item["rect"][3] - rect[1] - rect[3]) <= 6 and re.fullmatch(r"[0-9][0-9,.]*", item["text"].strip())]
    if not values:
        raise WDMSError("INVENTORY_VALUE_NOT_FOUND", f"未定位到{label}对应的数值控件，未触发查询。")
    left = min(item["rect"][0] for item in values)
    nearest = [item for item in values if item["rect"][0] == left]
    if len(nearest) != 1:
        raise WDMSError("INVENTORY_VALUE_AMBIGUOUS", f"{label}对应数值控件不唯一。")
    return nearest[0]



def query_controls_from_events(events: list) -> dict:
    texts = [item for item in events if item["event"] == "native_text"]
    buttons = [item for item in texts if item["class_name"].startswith("WindowsForms10.BUTTON.") and re.fullmatch(r"查询(?:\([A-Za-z]\))?", item["text"].replace("&", "").strip())]
    if len(buttons) != 1:
        raise WDMSError("QUERY_BUTTON_AMBIGUOUS", "未找到唯一的查询按钮，未触发任何查询。")
    controls = {"owner": owner_combo_from_events(events), "search": buttons[0]}
    for key, label in (("whole", "整件数"), ("loose", "散件数"), ("total", "在库数量合计")):
        # 在库数量合计只作诊断与刷新判据、不进入统计表：定位不到就跳过，不阻止采集；
        # 整件数、散件数仍必须唯一定位，否则不触发查询。
        try:
            controls[key] = inventory_value_control(texts, label)
        except WDMSError:
            if key != "total":
                raise
    return controls



def check_native_controls(api, main: dict, log: Path, max_nodes=256, max_depth=4, budget=4.0, read_labels=False) -> None:
    """Bounded native metadata and optional allowlisted label text; no UIA."""
    deadline = time.monotonic() + budget
    stack = [(main["hwnd"], 0)]
    seen = {main["hwnd"]}
    visited = 0
    events = []
    while stack:
        parent, depth = stack.pop()
        api.health(main["hwnd"], log, "native_metadata")
        for child in api.child_handles(parent):
            if time.monotonic() >= deadline or visited >= max_nodes:
                raise WDMSError("NATIVE_SCAN_LIMIT", "原生窗口检查达到时间或节点上限；结果不完整。")
            if child in seen:
                continue
            seen.add(child)
            record(log, "before_native_metadata", hwnd=child, depth=depth + 1)
            data = api.metadata(child)
            if data["pid"] != main["pid"]:
                raise WDMSError("WDMS_INSTANCE_CHANGED", "检查期间窗口所属进程变化。")
            visited += 1
            record(log, "native_control", depth=depth + 1, **data)
            events.append({"event": "native_control", "depth": depth + 1, **data})
            if read_labels and text_read_allowed(data):
                api.health(main["hwnd"], log, "before_label_text")
                text = api.read_label_text(data, log)
                record(log, "native_text", hwnd=child, class_name=data["class_name"], visible=data["visible"], rect=data.get("rect"), text=text)
                events.append({"event": "native_text", "hwnd": child, "class_name": data["class_name"],
                               "visible": data["visible"], "rect": data.get("rect"), "text": text})
                api.health(main["hwnd"], log, "after_label_text")
            if depth + 1 < max_depth and not any(word in data["class_name"].lower() for word in ("grid", "listview", "treeview")):
                stack.append((child, depth + 1))
    api.health(main["hwnd"], log, "after_native_metadata")
    record(log, "native_scan_complete", nodes=visited)
    return events


class NativeQuerySession:
    """Translate a native control snapshot into the platform-neutral query port."""
    def __init__(self, api, main, controls, log, expected_index=None, request_guard=None):
        self.api = api
        self.main = main
        self.controls = {key: dict(value) for key, value in controls.items()}
        self.log = log
        self.expected_index = expected_index
        self.request_guard = request_guard

    def validate_controls(self):
        for key, data in list(self.controls.items()):
            current = self.api.metadata(data["hwnd"])
            if current["pid"] != self.main["pid"] or current["class_name"] != data["class_name"]:
                if key == "total":
                    del self.controls[key]
                    record(self.log, "query_diagnostic_unavailable", field="total", reason="control_changed")
                    continue
                raise WDMSError("WDMS_INSTANCE_CHANGED", "查询前目标控件已变化，未查询。")
            if not current["visible"]:
                if key == "total":
                    del self.controls[key]
                    continue
                raise WDMSError("CONTROL_NOT_VISIBLE", "查询前目标控件不可见，未查询。")
            self.controls[key] = {**data, **current}

    def owner(self):
        data = self.controls["owner"]
        live = self.api.metadata(data["hwnd"])
        if live["pid"] != self.main["pid"] or live["class_name"] != data["class_name"] or not live["visible"]:
            raise WDMSError("OWNER_CONTROL_CHANGED", "读取期间货主控件身份变化，停止读取。")
        return self.api.read_label_text(self.controls["owner"], self.log)

    def counts(self):
        for key in ("whole", "loose"):
            data = self.controls[key]
            live = self.api.metadata(data["hwnd"])
            if live["pid"] != self.main["pid"] or live["class_name"] != data["class_name"] or not live["visible"]:
                raise WDMSError("QUERY_TARGET_CHANGED", "采样期间必需数量控件身份变化，停止读取。")
        return read_inventory_values(self.api, self.controls, self.log)

    def cursor_state(self):
        return self.api.cursor_state()

    def guard_observation(self):
        button = self.controls["search"]
        u = self.api.user32
        if not u.IsWindow(button["hwnd"]):
            raise WDMSError("QUERY_WINDOW_CHANGED", "观察期间查询按钮失效。")
        live = self.api.metadata(button["hwnd"])
        root = self.api.metadata(self.main["hwnd"])
        if root["pid"] != self.main["pid"] or not root["visible"] or not u.IsWindowEnabled(self.main["hwnd"]):
            raise WDMSError("QUERY_TARGET_CHANGED", "观察期间主窗口身份变化。")
        if (live["pid"] != self.main["pid"] or not live["visible"]
                or live["class_name"] != button["class_name"]):
            raise WDMSError("QUERY_TARGET_CHANGED", "观察期间查询按钮身份变化。")
        if int(u.GetForegroundWindow() or 0) != self.main["hwnd"]:
            raise WDMSError("QUERY_FOREGROUND_CHANGED", "观察期间WDMS离开前台，停止且未重试查询。")

    def guard_cursor(self):
        self.guard_observation()
        point = wintypes.POINT()
        u = self.api.user32
        if not u.GetCursorPos(ctypes.byref(point)) or int(u.WindowFromPoint(point) or 0) != self.controls["search"]["hwnd"]:
            raise WDMSError("QUERY_CURSOR_CHANGED", "指针已离开查询按钮，不能继续使用光标判断。")

    def foreground_hwnd(self):
        return int(self.api.user32.GetForegroundWindow() or 0)

    def button_enabled(self):
        return bool(self.api.user32.IsWindowEnabled(self.controls["search"]["hwnd"]))

    def health(self, stage):
        self.api.health(self.main["hwnd"], self.log, stage)

    def input_description(self, trigger):
        button = self.controls["search"]["hwnd"]
        parent = int(self.api.user32.GetParent(button) or 0)
        if not parent or self.api.metadata(parent)["pid"] != self.main["pid"]:
            raise WDMSError("QUERY_PARENT_INVALID", "查询按钮的父窗口无法验证，未查询。")
        control_id = self.api.user32.GetDlgCtrlID(button)
        if control_id < 0:
            raise WDMSError("QUERY_CONTROL_ID_INVALID", "查询按钮控件ID无效，未查询。")
        return {"search_hwnd": button, "parent_hwnd": parent, "control_id": control_id,
                "action_strategy": "sendinput_alt_f" if trigger == "alt-f" else "sendinput_button_center"}

    def prepare_input(self, owner, check):
        def guard():
            check()
            if self.request_guard is not None:
                self.request_guard()
            data = self.controls["owner"]
            live = self.api.metadata(data["hwnd"])
            if (live["pid"] != self.main["pid"] or live["class_name"] != data["class_name"]
                    or not live["visible"] or self.owner() != owner):
                raise WDMSError("QUERY_OWNER_MISMATCH", "输入前选中货主已变化，未发送查询。")
            if self.expected_index is not None:
                if combo_send(self.api, data["hwnd"], 0x0147) != self.expected_index:
                    raise WDMSError("QUERY_OWNER_MISMATCH", "输入前货主索引已变化，未发送查询。")
            check()
        self.api.query_input_guard = guard

    def send(self, trigger):
        sender = self.api.query_shortcut_input if trigger == "alt-f" else self.api.click_query_input
        sender(self.main, self.controls["search"]["hwnd"], self.log)


class TrialNativeWindows(NativeWindows):
    def health(self, hwnd, log, stage):
        # Retry only read-only WM_NULL, never the query or owner selection.
        deadline = time.monotonic() + 15.0
        context = getattr(self, "phase_context", None)
        if context is not None:
            deadline = min(deadline, context.deadline)
        attempts = 0
        while True:
            if context is not None:
                context.check()
            attempts += 1
            try:
                super().health(hwnd, log, stage)
                if attempts > 1:
                    record(log, 'health_recovered', stage=stage, attempts=attempts)
                return
            except WDMSError as error:
                if error.code not in ('WDMS_HEALTH_CHECK_FAILED', 'WDMS_UNRESPONSIVE'):
                    raise
                if time.monotonic() >= deadline:
                    raise WDMSError('WDMS_HEALTH_WAIT_TIMEOUT', 'WDMS 探活持续未恢复，已等待约 15 秒；停止测试，没有重复查询。') from error
                record(log, 'health_wait', stage=stage, attempt=attempts, **error_info(error))
                time.sleep(0.2)


def message_policy(api) -> NativeMessagePolicy:
    """Synthetic API fixtures and real native APIs share the same defaults."""
    return getattr(api, 'message_policy', DEFAULT_QUERY_POLICY.messages)


def combo_send(api, hwnd: int, message: int, parameter: int = 0) -> int:
    result = ctypes.c_size_t()
    ok = api.user32.SendMessageTimeoutW(hwnd, message, parameter, 0, 0x23, message_policy(api).combo_select_ms, ctypes.byref(result))
    if not ok:
        raise WDMSError('OWNER_SWITCH_FAILED', '货主下拉框操作未在时限内完成。')
    return ctypes.c_ssize_t(result.value).value


def select_owner(api, main, combo, index, expected, log, *, send=None):
    send = send or combo_send
    hwnd = combo['hwnd']
    live = api.metadata(hwnd)
    if live['pid'] != main['pid'] or live['class_name'] != combo['class_name'] or not live['visible']:
        raise WDMSError('OWNER_CONTROL_CHANGED', '货主下拉框身份变化，未切换。')
    api.health(main['hwnd'], log, 'before_owner_switch')
    parent = int(api.user32.GetParent(hwnd) or 0)
    if not parent or api.metadata(parent)['pid'] != main['pid']:
        raise WDMSError('OWNER_PARENT_INVALID', '货主下拉框父窗口无法验证。')
    control_id = api.user32.GetDlgCtrlID(hwnd)
    if control_id < 0:
        raise WDMSError('OWNER_CONTROL_ID_INVALID', '货主下拉框控件 ID 不在有效范围。')
    current = send(api, hwnd, 0x0147)  # CB_GETCURSEL
    if current != index:
        record(log, 'before_owner_switch', index=index, expected_owner=expected)
        live = api.metadata(hwnd)
        if live['pid'] != main['pid'] or live['class_name'] != combo['class_name'] or not live['visible']:
            raise WDMSError('OWNER_CONTROL_CHANGED', '日志记录后货主控件身份变化，未切换。')
        if send(api, hwnd, 0x014e, index) != index:  # CB_SETCURSEL
            raise WDMSError('OWNER_SWITCH_FAILED', '未能设置指定货主，不重试。')
        # One normal CBN_SELCHANGE notification, after setting the native selection.
        result = ctypes.c_size_t()
        if not api.user32.SendMessageTimeoutW(parent, 0x0111, (1 << 16) | (control_id & 0xffff),
                                              hwnd, 0x23, message_policy(api).owner_notify_ms, ctypes.byref(result)):
            raise WDMSError('OWNER_SWITCH_FAILED', '货主选择通知未完成，不重试、不查询。')
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if send(api, hwnd, 0x0147) == index and api.read_label_text({**combo, **live}, log) == expected:
            record(log, 'owner_selection_verified', index=index, owner=expected,
                         result_owner_independently_verified=False)
            return
        time.sleep(0.05)
    raise WDMSError('OWNER_SELECTION_MISMATCH', '选中货主未与目标名称及索引一致，不查询。')


class NativeTrialClient:
    """Own the native session; expose collection operations without HWNDs."""
    def __init__(self, log, title, *, api_factory=TrialNativeWindows, scanner=check_native_controls,
                 selector=select_owner, environment_check=None,
                 message_policy: NativeMessagePolicy = DEFAULT_QUERY_POLICY.messages) -> None:
        self.log = log
        self.title = title
        self.api_factory = api_factory
        self.scanner = scanner
        self.selector = selector
        self.environment_check = environment_check
        self.message_policy = message_policy
        self.api = None
        self.context = None

    def bind_context(self, context):
        self.context = context
        if self.api is not None:
            self.api.phase_context = context

    def connect(self, document):
        if self.environment_check is not None:
            self.environment_check()
        else:
            from adapters.windows_environment import inspect_environment, require_usable_environment
            require_usable_environment(inspect_environment())
        self.context.check()
        self.api = self.api_factory()
        self.api.message_policy = self.message_policy
        self.api.phase_context = self.context
        matches = self.api.find_main(self.title, self.log)
        if len(matches) != 1 or matches[0]['minimized']:
            raise WDMSError('WDMS_WINDOW_INVALID', '需要唯一且未最小化的WDMS主窗口。')
        self.main = matches[0]
        identity = document.get('identity')
        if identity is not None and (self.main['pid'] != identity['pid']
                or self.main['hwnd'] != identity['main_hwnd']
                or not self.api.user32.IsWindowEnabled(self.main['hwnd'])):
            raise WDMSError('OWNER_SESSION_CHANGED', '读取名单后WDMS会话发生变化，未选择或查询货主。')
        self.api.health(self.main['hwnd'], self.log, 'trial_initial')

    def locate_controls(self, budget):
        deadline = min(self.context.deadline, time.monotonic() + budget)
        attempts = 0
        while True:
            self.context.check()
            attempts += 1
            try:
                events = self.scanner(self.api, self.main, self.log, max_depth=8, read_labels=True,
                                      budget=max(0.0, deadline-time.monotonic()))
                self.controls = query_controls_from_events(events)
                if attempts > 1:
                    record(self.log, "collection_scan_recovered", attempts=attempts)
                return
            except NativeTextReadError as error:
                if error.winerror != 1460 or time.monotonic() >= deadline:
                    raise
                record(self.log, "collection_scan_deferred", attempt=attempts, **error_info(error))
                time.sleep(min(0.2, max(0.0, deadline-time.monotonic())))

    def read_names(self):
        return self.api.read_owner_list(self.controls['owner'], self.log)

    def select_owner(self, index, owner):
        self.selector(self.api, self.main, self.controls['owner'], index, owner, self.log)

    def place_query_cursor(self):
        self.api.user32.SetForegroundWindow(self.main['hwnd'])
        time.sleep(0.2)
        rect = self.api.metadata(self.controls['search']['hwnd'])['rect']
        point = wintypes.POINT((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
        if int(self.api.user32.WindowFromPoint(point) or 0) != self.controls['search']['hwnd']:
            raise WDMSError('OWNER_CONTROL_OBSCURED', '查询按钮被遮挡，未查询。')
        if not self.api.user32.SetCursorPos(point.x, point.y):
            raise WDMSError('QUERY_CURSOR_FAILED', '无法将指针移到WDMS，未查询。')
        time.sleep(0.2)

    def query_session(self, index):
        return NativeQuerySession(self.api, self.main, self.controls, self.log, index,
                                  getattr(self, "request_guard", None))
