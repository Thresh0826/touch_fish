"""Assemble a Windows x64 runtime ZIP on any host; never execute/install its Python."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import struct
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "WDMS_Cloud_Project"
ASSETS = (
    {"name": "cpython", "version": "3.12.14", "filename": "cpython-3.12.14-windows-x64.tar.gz",
     "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
     "size": 21980728, "sha256": "7c45c9622400d578709a9b2cddbe8124cc21d382409d9f13406d706d28e31b14"},
    {"name": "openpyxl", "version": "3.1.5", "filename": "openpyxl-3.1.5-py2.py3-none-any.whl",
     "url": "https://files.pythonhosted.org/packages/c0/da/977ded879c29cbd04de313843e76868e6e13408a94ed6b987245dc7c8506/openpyxl-3.1.5-py2.py3-none-any.whl",
     "size": 250910, "sha256": "5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2"},
    {"name": "et-xmlfile", "version": "2.0.0", "filename": "et_xmlfile-2.0.0-py3-none-any.whl",
     "url": "https://files.pythonhosted.org/packages/c1/8b/5fe2cc11fee489817272089c4203e679c63b570a5aaeb18d852ae3cbba6a/et_xmlfile-2.0.0-py3-none-any.whl",
     "size": 18059, "sha256": "7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa"},
)
CRITICAL = (
    "python.exe", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll", "Lib/tkinter/__init__.py",
    "tcl/tcl8.6/init.tcl", "tcl/tk8.6/tk.tcl", "Lib/site-packages/openpyxl/__init__.py",
    "Lib/site-packages/et_xmlfile/__init__.py",
)


def info(data):
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def checked_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or any(c in name for c in "\\:"):
        raise ValueError("unsafe archive path")
    return path


def pe_machine(data):
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("missing Windows PE image")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset+6 > len(data) or data[offset:offset+4] != b"PE\0\0":
        raise ValueError("invalid Windows PE image")
    return struct.unpack_from("<H", data, offset+4)[0]


def cached_asset(cache, asset, *, download=False):
    cache = Path(cache)
    path = cache/asset["filename"]
    if not path.exists():
        if not download:
            raise ValueError("missing cached portable asset: "+asset["filename"])
        cache.mkdir(parents=True, exist_ok=True)
        # Only explicit maintainer builds download; field startup never calls here.
        with urllib.request.urlopen(asset["url"], timeout=30) as source, path.open("xb") as target:
            size = 0
            while True:
                chunk = source.read(1024*1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > asset["size"]:
                    raise ValueError("asset is larger than pinned size")
                target.write(chunk)
    if path.is_symlink() or info(path.read_bytes()) != {"size": asset["size"], "sha256": asset["sha256"]}:
        raise ValueError("cached asset digest mismatch; preserve the cache and choose a fresh folder")
    return path


def runtime_files(cache, *, download=False):
    files = {}
    runtime = cached_asset(cache, ASSETS[0], download=download)
    with tarfile.open(runtime, "r:gz") as archive:
        for member in archive.getmembers():
            path = checked_name(member.name)
            if path.parts[0] != "python" or member.issym() or member.islnk():
                raise ValueError("unexpected runtime archive member")
            if member.isdir():
                continue
            if not member.isfile() or member.size > 64*1024*1024:
                raise ValueError("unexpected runtime object")
            name = str(path.relative_to("python"))
            if name in files:
                raise ValueError("duplicate runtime member")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("missing runtime data")
            with source:
                files[name] = source.read()
    for asset in ASSETS[1:]:
        wheel = cached_asset(cache, asset, download=download)
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip() is not None:
                raise ValueError("wheel CRC failed")
            for member in archive.infolist():
                path = checked_name(member.filename)
                if member.is_dir():
                    continue
                if ".data" in path.parts[0] or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("unsupported wheel layout")
                name = "Lib/site-packages/"+str(path)
                if name in files:
                    raise ValueError("wheel overwrites another runtime member")
                files[name] = archive.read(member)
    if not set(CRITICAL) <= set(files) or "LICENSE.txt" not in files:
        raise ValueError("Python/Tk/runtime/library/license payload incomplete")
    for name in CRITICAL:
        if name.endswith((".exe", ".dll", ".pyd")) and pe_machine(files[name]) != 0x8664:
            raise ValueError("runtime binary is not Windows AMD64: "+name)
    return files


def verify(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError("duplicate member or bad ZIP CRC")
        for member in archive.infolist():
            checked_name(member.filename)
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("symlink in portable ZIP")
        manifest = json.loads(archive.read("PORTABLE_MANIFEST.json"))
        if (manifest.get("kind") != "source-with-windows-runtime" or manifest.get("windows_verified") is not False
                or manifest.get("windows_runtime_executed") is not False
                or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", ""))):
            raise ValueError("invalid portable manifest")
        if set(names) != set(manifest["files"]) | {"PORTABLE_MANIFEST.json"}:
            raise ValueError("portable member mismatch")
        for name, expected in manifest["files"].items():
            if info(archive.read(name)) != expected:
                raise ValueError("portable size/SHA256 mismatch: "+name)
        marker = json.loads(archive.read(PROJECT+"/PORTABLE_RUNTIME.json"))
        if set(marker["critical"]) != set(CRITICAL) or marker["target"] != "windows-x86_64":
            raise ValueError("missing portable startup payload")
        for name in CRITICAL:
            data = archive.read(PROJECT+"/runtime/"+name)
            if info(data) != marker["critical"][name]:
                raise ValueError("critical runtime hash mismatch")
            if name.endswith((".exe", ".dll", ".pyd")) and pe_machine(data) != 0x8664:
                raise ValueError("wrong runtime architecture")
        source_manifest = json.loads(archive.read("SOURCE_MANIFEST.json"))
        if source_manifest["source_commit"] != manifest["source_commit"]:
            raise ValueError("source version mismatch")
        for name, expected in source_manifest["files"].items():
            if manifest["files"].get(name) != expected:
                raise ValueError("source snapshot mismatch")
    return manifest


def build(source_package, cache, destination=None, *, download=False):
    spec = importlib.util.spec_from_file_location("moyu_source_packager", ROOT/"工具"/"打包摸鱼小工具.py")
    source_packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source_packager)
    source_manifest = source_packager.verify(source_package)
    files = runtime_files(cache, download=download)
    marker = {"schema": 1, "target": "windows-x86_64", "python_version": "3.12.14",
              "upstream_assets": list(ASSETS), "critical": {name: info(files[name]) for name in CRITICAL}}
    with zipfile.ZipFile(source_package) as source:
        payload = {name: source.read(name) for name in source.namelist()}
    payload.update({PROJECT+"/runtime/"+name: data for name, data in files.items()})
    payload[PROJECT+"/PORTABLE_RUNTIME.json"] = (json.dumps(marker, ensure_ascii=False, indent=2)+"\n").encode("utf-8")
    # ASCII command text avoids dependence on the active Windows console code page.
    payload["启动摸鱼小工具.bat"] = (
        '@echo off\r\nsetlocal\r\ncd /d "%~dp0WDMS_Cloud_Project"\r\n'
        'powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0WDMS_Cloud_Project\\moyu_tools\\launch.ps1"\r\n'
        'if errorlevel 1 pause\r\n').encode("ascii")
    instructions = (ROOT/PROJECT/"docs"/"moyu_portable.md").read_bytes()
    if payload.get(PROJECT+"/docs/moyu_portable.md") != instructions:
        raise ValueError("portable instructions differ from saved source snapshot")
    payload["带环境便携包使用说明.md"] = instructions
    commit = source_manifest["source_commit"]
    manifest = {"schema": 1, "kind": "source-with-windows-runtime", "product": "摸鱼小工具",
                "source_commit": commit, "target": "Windows 10/11 x64", "python_version": "3.12.14",
                "windows_verified": False, "windows_runtime_executed": False,
                "upstream_assets": list(ASSETS), "files": {name: info(data) for name, data in sorted(payload.items())}}
    folder = Path(destination) if destination else ROOT/"导出"/"摸鱼小工具"/("moyu-portable_"+commit[:8])
    folder.mkdir(parents=True, exist_ok=True)
    output = folder/"摸鱼小工具带环境便携包.zip"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            archive.writestr(name, data)
        archive.writestr("PORTABLE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    verify(output)
    (folder/"PORTABLE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (folder/"SOURCE_MANIFEST.json").write_text(json.dumps(source_manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (folder/"带环境便携包使用说明.md").write_bytes(instructions)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    (folder/(output.name+".sha256")).write_text(checksum+"  "+output.name+"\n", encoding="utf-8")
    return {"package": str(output), "source_commit": commit, "sha256": checksum,
            "bytes": output.stat().st_size, "members": len(payload)+1, "windows_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--cache", type=Path, default=ROOT/"导出"/"摸鱼便携依赖缓存")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--download", action="store_true", help="maintainer-only: fetch pinned public runtime assets")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = {"verified": str(args.verify), "source_commit": verify(args.verify)["source_commit"]}
    else:
        if args.source is None:
            parser.error("--source is required")
        result = build(args.source, args.cache, args.output, download=args.download)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
