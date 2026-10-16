"""Bounded ZIP verification and no-overwrite publication, with restart receipts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import zipfile
import zlib

from moyu_tools.model import fail, zip_name


def inspect_zip(path, check=lambda: None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        fail("MOYU_COPY_PENDING", "暂存ZIP尚未完整出现。")
    before = path.stat()
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            check()
            sha.update(chunk)
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                check()
                # Read to EOF for CRC; do not extract, decompress to disk or execute.
                with archive.open(info) as member:
                    while member.read(1024 * 1024):
                        check()
    except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error):
        fail("MOYU_ZIP_INVALID", "ZIP尚未完整、CRC错误或需要密码；保留暂存文件，未发布。")
    check()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        fail("MOYU_COPY_PENDING", "校验过程中ZIP仍在变化，继续等待。")
    return {"name": zip_name(path.name), "size": after.st_size, "sha256": sha.hexdigest()}


def matches(path, receipt, check=lambda: None):
    try:
        return inspect_zip(path, check) == receipt
    except (OSError, zipfile.BadZipFile):
        return False


def wait_file(folder, name, expected_size, context, *, clock, sleep, stable_seconds):
    target = Path(folder) / zip_name(name)
    previous = None
    stable_since = None
    last_error = None
    while True:
        context.check()
        present = list(Path(folder).iterdir())
        if any(p.name != name for p in present):
            fail("MOYU_COPY_AMBIGUOUS", "暂存目录出现额外文件，保留现场并停止。")
        try:
            stat = target.stat()
            key = (stat.st_size, stat.st_mtime_ns)
            if key != previous:
                stable_since = clock()
                previous = key
            elif stable_since is not None and clock() - stable_since >= stable_seconds:
                if expected_size is None or stat.st_size == expected_size:
                    try:
                        return inspect_zip(target, context.check)
                    except Exception as error:
                        if getattr(error, "code", None) not in ("MOYU_ZIP_INVALID", "MOYU_COPY_PENDING"):
                            raise
                        last_error = error
        except (FileNotFoundError, PermissionError):
            # Explorer/antivirus can still be writing; bounded by the shared deadline.
            stable_since = None
        if context.remaining() <= 0.1 and last_error is not None:
            raise last_error
        sleep(min(0.2, context.remaining()))


def publish(staging, output, receipt, existing_receipt=None, check=lambda: None):
    name = zip_name(receipt["name"])
    source, destination = Path(staging) / name, Path(output) / name
    check()
    if destination.exists() or destination.is_symlink():
        if existing_receipt == receipt and matches(destination, receipt, check):
            return receipt
        fail("MOYU_NAME_CONFLICT", "目标文件夹已有同名文件，未覆盖；请处理冲突后继续。")
    if not matches(source, receipt, check):
        fail("MOYU_COPY_CHANGED", "暂存文件已变化，未发布。")
    check()
    # Windows rename never replaces an existing destination. On POSIX, link is
    # the exclusive publication primitive. Staging is on the destination volume.
    try:
        if os.name == "nt":
            os.rename(source, destination)
        else:
            os.link(source, destination)
    except FileExistsError:
        fail("MOYU_NAME_CONFLICT", "发布时发现同名文件，未覆盖。")
    except OSError as error:
        fail("MOYU_PUBLISH_UNSUPPORTED", "目标文件系统不支持安全发布；保留暂存ZIP，未改写已有文件。")
    if not matches(destination, receipt, check):
        fail("MOYU_COPY_CHANGED", "发布后的文件未通过回读校验，保留文件并停止。")
    return receipt
