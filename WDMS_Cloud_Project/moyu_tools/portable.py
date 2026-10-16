"""Read-only checks for the bundled Windows runtime; never installs packages."""
from __future__ import annotations

import hashlib
import importlib
from importlib.metadata import version
import json
from pathlib import Path, PurePosixPath
import struct
import sys

from moyu_tools.model import fail

CRITICAL = (
    "python.exe", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
    "Lib/tkinter/__init__.py", "tcl/tcl8.6/init.tcl", "tcl/tk8.6/tk.tcl",
    "Lib/site-packages/openpyxl/__init__.py", "Lib/site-packages/et_xmlfile/__init__.py",
)


def verify_critical(runtime, metadata):
    runtime = Path(runtime).resolve()
    if (metadata.get("schema") != 1 or metadata.get("target") != "windows-x86_64"
            or not isinstance(metadata.get("critical"), dict) or set(metadata["critical"]) != set(CRITICAL)):
        fail("MOYU_RUNTIME_INVALID", "便携环境清单不完整，请保留旧目录并重新完整解压带环境便携包。")
    for name in CRITICAL:
        item = metadata["critical"][name]
        path = runtime.joinpath(*PurePosixPath(name).parts)
        if (not path.is_file() or path.is_symlink() or runtime not in path.resolve().parents
                or path.stat().st_size != item.get("size")
                or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256")):
            fail("MOYU_RUNTIME_INCOMPLETE", "包内Python、界面库或表格库文件缺失或变化；未改用其他Python，请重新完整解压便携包。")


def check(project):
    project = Path(project).resolve()
    marker = project/"PORTABLE_RUNTIME.json"
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    runtime = project/"runtime"
    verify_critical(runtime, metadata)
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.getwindowsversion().major < 10:
        fail("MOYU_RUNTIME_PLATFORM", "本便携包适用于Windows 10/11 64位系统。")
    if Path(sys.executable).resolve() != (runtime/"python.exe").resolve():
        fail("MOYU_RUNTIME_WRONG_PYTHON", "当前没有使用包内Python，未启动业务操作。请双击本包的启动摸鱼小工具.bat。")
    if ".".join(str(x) for x in sys.version_info[:3]) != metadata["python_version"]:
        fail("MOYU_RUNTIME_VERSION", "Python版本与便携包清单不一致，请重新完整解压。")
    for module, expected in (("openpyxl", "3.1.5"), ("et_xmlfile", "2.0.0"), ("tkinter", None)):
        loaded = importlib.import_module(module)
        if runtime.resolve() not in Path(loaded.__file__).resolve().parents:
            fail("MOYU_RUNTIME_MIXED", "检测到包外依赖，未混用电脑上的其他Python环境。")
        if expected is not None and version(module.replace("_", "-")) != expected:
            fail("MOYU_RUNTIME_MIXED", "包内表格依赖版本不符，请重新完整解压。")
    import tkinter
    tcl = tkinter.Tcl()
    return {"python": metadata["python_version"], "bits": 64, "tcl": tcl.eval("info patchlevel"),
            "openpyxl": version("openpyxl"), "et_xmlfile": version("et-xmlfile"),
            "bundled_runtime_checked": True, "business_windows_verified": False}
