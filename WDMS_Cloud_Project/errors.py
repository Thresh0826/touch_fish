"""Stable, safe-to-display task errors."""


class WDMSError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NativeTextReadError(WDMSError):
    """Keep native read failure metadata without retaining control contents."""

    def __init__(self, code, message, *, winerror, hwnd, class_name):
        super().__init__(code, message)
        self.winerror = winerror
        self.hwnd = hwnd
        self.class_name = class_name


class TaskAlreadyRunning(WDMSError):
    def __init__(self):
        super().__init__("TASK_ALREADY_RUNNING", "已有采集任务正在运行。")


class GracefulStop(WDMSError):
    def __init__(self, signal_name: str = "中断信号"):
        super().__init__("TASK_INTERRUPTED", "任务收到中断信号，已停止本次采集。")
        self.signal_name = signal_name
