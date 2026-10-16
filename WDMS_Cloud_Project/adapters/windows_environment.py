"""Read-only process-token diagnostics; never alter tokens or directory labels."""

from __future__ import annotations

import sys
from typing import Any, Dict

from errors import WDMSError


def inspect_environment() -> Dict[str, Any]:
    if sys.platform != "win32":
        raise WDMSError("REAL_MODE_WINDOWS_ONLY", "运行环境自检只能在 Windows 上执行。")
    try:
        import ctypes
        from ctypes import wintypes
        import psutil

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.GetTokenInformation.restype = wintypes.BOOL
        advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            # TokenIntegrityLevel = 25. The first call obtains the buffer size.
            advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, 25, buffer, size.value, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            label = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents
            count = advapi.GetSidSubAuthorityCount(label.Sid).contents.value
            if not count:
                raise ValueError("integrity SID has no subauthority")
            rid = advapi.GetSidSubAuthority(label.Sid, count - 1).contents.value
            restricted = wintypes.DWORD()
            if not advapi.GetTokenInformation(token, 21, ctypes.byref(restricted), ctypes.sizeof(restricted), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(token)
        level = "Untrusted" if rid < 4096 else "Low" if rid < 8192 else "Medium" if rid < 12288 else "High" if rid < 16384 else "System"
        visible_count = len(psutil.pids())
        return {
            "integrity_level": level, "integrity_rid": rid,
            "token_has_restrictions": bool(restricted.value),
            "visible_process_count": visible_count,
            "limited_process_visibility_warning": visible_count < 20,
            "python_executable": sys.executable,
        }
    except ImportError as error:
        raise WDMSError("WINDOWS_DEPENDENCY_MISSING", "环境自检需要安装 requirements-windows.txt。") from error
    except Exception as error:
        code = getattr(error, "winerror", None)
        suffix = f" WinError={code}" if isinstance(code, int) else ""
        raise WDMSError("ENV_CHECK_FAILED", "无法读取当前进程的 Windows 令牌信息。" + suffix) from error


def require_usable_environment(info: Dict[str, Any]) -> None:
    if info["integrity_rid"] < 8192:
        raise WDMSError(
            "ENV_INTEGRITY_TOO_LOW",
            f"当前 Python 进程完整性为 {info['integrity_level']}，无法可靠操作 WDMS。"
            "请在助手工作区外的普通用户目录重新建立虚拟环境，并从普通 PowerShell 运行；"
            "不要通过提权或修改工作区安全标签绕过限制。",
        )
    # A small process count alone does not prove a restricted token. Report it
    # as a warning rather than refusing valid machines with few processes.
