"""Create a separate, allowlisted personal-tool source ZIP. Never modify old packages."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "WDMS_Cloud_Project"
MANIFEST = "SOURCE_MANIFEST.json"
INSTRUCTIONS = "摸鱼小工具操作说明.md"
DIRECTORIES = {"adapters", "core", "desktop_demo", "services", "scripts", "tests", "test_logs", "assets", "docs", "moyu_tools"}
EXTENSIONS = {".py", ".md", ".txt", ".bat", ".ps1", ".cs", ".png", ".json"}
REQUIRED = {
    SOURCE+"/scripts/MoyuTools.py", SOURCE+"/启动摸鱼小工具.bat", SOURCE+"/moyu_tools/app.py",
    SOURCE+"/moyu_tools/engine.py", SOURCE+"/moyu_tools/native.py", SOURCE+"/moyu_tools/store.py",
    SOURCE+"/moyu_tools/files.py", SOURCE+"/moyu_tools/bridge.py", SOURCE+"/moyu_tools/runtime.py",
    SOURCE+"/moyu_tools/settings.py", SOURCE+"/moyu_tools/model.py", SOURCE+"/moyu_tools/wizard.py",
    SOURCE+"/moyu_tools/sheet.py", SOURCE+"/tests/test_moyu_sheet_rows.py",
    SOURCE+"/moyu_tools/portable.py", SOURCE+"/scripts/check_moyu_runtime.py",
    SOURCE+"/moyu_tools/calibration.py", SOURCE+"/moyu_tools/direct_copy.py",
    SOURCE+"/tests/test_moyu_simple.py",
    SOURCE+"/docs/moyu_portable.md", SOURCE+"/tests/test_moyu_portable.py",
    SOURCE+"/moyu_tools/windows_native.cs", SOURCE+"/moyu_tools/windows_bridge.ps1", SOURCE+"/moyu_tools/launch.ps1",
    SOURCE+"/tests/test_moyu_engine.py", SOURCE+"/tests/test_moyu_native.py", SOURCE+"/tests/test_moyu_runtime.py",
    SOURCE+"/tests/moyu_fixtures.py", SOURCE+"/docs/moyu_tools.md",
    "工具/打包摸鱼小工具.py",
    "工具/打包摸鱼便携包.py",
}


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments]).decode("utf-8").strip()


def summary(data):
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def verify(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError("duplicate member or invalid CRC")
        for name in names:
            parsed = PurePosixPath(name)
            if parsed.is_absolute() or ".." in parsed.parts or any(c in name for c in "\\:"):
                raise ValueError("unsafe archive path")
            mode = archive.getinfo(name).external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError("symlink in archive")
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
        if manifest.get("windows_verified") is not False or manifest.get("product") != "摸鱼小工具":
            raise ValueError("missing unverified product declaration")
        files = manifest["files"]
        if set(names) != set(files) | {MANIFEST} or not REQUIRED <= set(files):
            raise ValueError("manifest or required member mismatch")
        for name, expected in files.items():
            if summary(archive.read(name)) != expected:
                raise ValueError("size/SHA256 mismatch: "+name)
    return manifest


def build(destination=None):
    if git("status", "--porcelain"):
        raise RuntimeError("Save source with 项目版本管理.py before packaging; worktree is not clean")
    commit = git("rev-parse", "HEAD")
    files = {}
    for name in git("ls-files", "-z", "--", SOURCE).split("\0"):
        if not name:
            continue
        relative = PurePosixPath(name)
        inner = relative.relative_to(SOURCE)
        if len(inner.parts) > 1 and inner.parts[0] not in DIRECTORIES:
            raise ValueError("unexpected source directory: "+name)
        if relative.suffix.lower() not in EXTENSIONS and str(inner) != ".gitignore":
            raise ValueError("unexpected source extension: "+name)
        if relative.suffix.lower() == ".json" and not name.endswith(".sample.json"):
            raise ValueError("non-sample JSON not allowed: "+name)
        source = ROOT/name
        if source.is_symlink():
            raise ValueError("source symlink not allowed")
        data = source.read_bytes()
        tracked = subprocess.check_output(["git", "-C", str(ROOT), "show", commit+":"+name])
        if tracked != data:
            raise ValueError("disk differs from saved source: "+name)
        files[name] = data
    for packager in ("工具/打包摸鱼小工具.py", "工具/打包摸鱼便携包.py"):
        data = (ROOT/packager).read_bytes()
        tracked = subprocess.check_output(["git", "-C", str(ROOT), "show", commit+":"+packager])
        if tracked != data:
            raise ValueError("packager differs from saved source")
        files[packager] = data
    if not REQUIRED <= set(files):
        raise ValueError("required personal-tool source missing")
    files[INSTRUCTIONS] = files[SOURCE+"/docs/moyu_tools.md"]
    files["启动摸鱼小工具.bat"] = (
        '@echo off\r\nsetlocal\r\ncd /d "%~dp0WDMS_Cloud_Project"\r\n'
        'powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0WDMS_Cloud_Project\\moyu_tools\\launch.ps1"\r\n'
        'if errorlevel 1 pause\r\n').encode("ascii")
    manifest = {"schema": 1, "product": "摸鱼小工具", "revision": "moyu-v3", "bundled_dependencies": False,
                "source_commit": commit, "branch": git("branch", "--show-current"),
                "windows_verified": False, "native_windows_compilation_verified": False,
                "files": {name: summary(data) for name, data in sorted(files.items())}}
    folder = Path(destination) if destination else ROOT/"导出"/"摸鱼小工具"/("moyu-v3_"+commit[:8])
    folder.mkdir(parents=True, exist_ok=True)
    package = folder/"摸鱼小工具源码更新包.zip"
    # Exclusive output: existing delivery packages are never replaced.
    with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    verify(package)
    (folder/MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (folder/INSTRUCTIONS).write_bytes(files[INSTRUCTIONS])
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    (folder/(package.name+".sha256")).write_text(checksum+"  "+package.name+"\n", encoding="utf-8")
    return {"package": str(package), "source_commit": commit, "sha256": checksum,
            "members": len(files)+1, "windows_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.verify:
        manifest = verify(arguments.verify)
        result = {"verified": str(arguments.verify), "source_commit": manifest["source_commit"], "members": len(manifest["files"])+1}
    else:
        result = build(arguments.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
