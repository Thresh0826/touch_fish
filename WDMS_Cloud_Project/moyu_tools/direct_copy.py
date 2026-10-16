"""Direct Explorer paste reconciliation; no staging, publication or repeated CRC pass."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sys
import zipfile

from errors import WDMSError
from moyu_tools.model import fail, zip_name


def completed_reader(path):
    if sys.platform != "win32":
        return Path(path).open("rb")
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Share reads only. An active Explorer writer cannot pass this check merely
    # by leaving a temporarily stable or preallocated file size on disk.
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise PermissionError("copy still holds target")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


def inspect_target(path, check=lambda: None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        fail("MOYU_COPY_PENDING", "目标ZIP尚未完整出现。")
    before = path.stat()
    sha = hashlib.sha256()
    with completed_reader(path) as source:
        for chunk in iter(lambda: source.read(1024*1024), b""):
            check()
            sha.update(chunk)
        source.seek(0)
        # Confirm a complete ZIP directory, without extracting or rereading all
        # compressed members for CRC as the old staged publication did.
        try:
            with zipfile.ZipFile(source) as archive:
                archive.infolist()
        except (zipfile.BadZipFile, EOFError):
            fail("MOYU_COPY_PENDING", "目标ZIP尚未复制完整，继续等待。")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        fail("MOYU_COPY_PENDING", "目标ZIP仍在变化，继续等待。")
    check()
    return {"name": zip_name(path.name), "size": after.st_size, "sha256": sha.hexdigest()}


def target_matches(path, receipt, check=lambda: None):
    try:
        return inspect_target(path, check) == receipt
    except (OSError, zipfile.BadZipFile, WDMSError):
        return False


def destination(output, name):
    output = Path(output)
    name = zip_name(name)
    # Windows compares filenames without case; mirror that in synthetic tests.
    entries = [entry for entry in output.iterdir() if entry.name.casefold() == name.casefold()]
    if len(entries) > 1:
        fail("MOYU_NAME_CONFLICT", "目标文件夹有多个大小写同名文件，未粘贴。")
    return entries[0] if entries else output/name


def wait_target(path, expected_size, context, *, clock, sleep, stable_seconds):
    previous, since = None, None
    while True:
        context.check()
        try:
            stat = Path(path).stat()
            current = (stat.st_size, stat.st_mtime_ns)
            if current != previous:
                previous, since = current, clock()
            elif since is not None and clock()-since >= stable_seconds:
                if expected_size is None or stat.st_size == expected_size:
                    return inspect_target(path, context.check)
        except (FileNotFoundError, PermissionError):
            since = None
        except Exception as error:
            if getattr(error, "code", None) != "MOYU_COPY_PENDING":
                raise
        sleep(min(.2, context.remaining()))
