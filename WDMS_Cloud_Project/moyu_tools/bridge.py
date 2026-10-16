"""Bounded requests to one STA Windows helper, reusing the project supervisor."""
from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys

from adapters.supervisor import JsonLineWorker
from errors import WDMSError
from moyu_tools.model import fail
from services.event_log import record


class Bridge:
    def __init__(self, log=None):
        if sys.platform != "win32":
            fail("MOYU_WINDOWS_REQUIRED", "摸鱼小工具的现场操作仅支持Windows；未连接任何业务系统。")
        directory = Path(__file__).resolve().parent
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows"))/"System32"/"WindowsPowerShell"/"v1.0"/"powershell.exe"
        self.worker = JsonLineWorker([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                                      "-ExecutionPolicy", "Bypass", "-File", str(directory/"windows_bridge.ps1"),
                                      "-ParentProcess", str(os.getpid())], str(directory))
        self.started = False
        self.log = log

    def call(self, action, payload=None, context=None):
        if context:
            context.check()
        timeout = 20 if not self.started else 8
        if context:
            timeout = min(timeout, context.remaining())
        try:
            result = self.worker.request(action, payload or {}, timeout)
        except WDMSError as error:
            if self.log is not None:
                record(self.log, "moyu_native_failed", action=action, error_code=error.code, error=error.message)
            readonly = (action in ("read", "conditions", "ocr", "hash", "file", "text", "windows", "preview")
                        or action in ("date", "edit") and "value" not in (payload or {}))
            if error.code == "WORKER_HARD_TIMEOUT" and readonly:
                # Recreate only a terminated read helper for a future observation.
                # The original action is NOT repeated here; writes and inputs never retry.
                old = self.worker
                self.close()
                self.worker = JsonLineWorker(old.command, old.cwd)
                self.started = False
                fail("MOYU_READ_PENDING", "本次只读观察超时，继续在剩余期限内等待；没有重发业务操作。")
            raise
        self.started = True
        if context:
            context.check()
        return result

    def close(self):
        try:
            self.worker.close(0.5)
        finally:
            process = self.worker.process
            if process is not None and process.poll() is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            # A terminated child can have a broken pipe; never mask its receipt.
                            pass


class ParentHandle:
    """Stop accepting actions if the GUI/controller is forcibly closed."""
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x00100000, False, pid)
        if not self.handle:
            fail("MOYU_PARENT_UNAVAILABLE", "无法监督启动窗口，未开始自动操作。")

    def alive(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 258

    def close(self):
        self.kernel.CloseHandle(self.handle)
