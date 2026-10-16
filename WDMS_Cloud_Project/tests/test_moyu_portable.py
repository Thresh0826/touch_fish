"""Synthetic distribution integrity tests; Windows binaries are never executed."""
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from errors import WDMSError
from moyu_tools.portable import CRITICAL, check, verify_critical


def pe(bits=0x8664):
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, bits)
    return bytes(data)


class MoyuPortableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        cls.modules = []
        for name in ("打包摸鱼便携包", "打包摸鱼小工具"):
            spec = importlib.util.spec_from_file_location("portable_test_"+name, cls.root/"工具"/(name+".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls.modules.append(module)
        cls.pack, cls.source_pack = cls.modules

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.cache = self.folder/"cache"
        self.cache.mkdir()
        self.assets = []
        self.runtime_archive = self.cache/"runtime.tar.gz"
        self.make_runtime()
        for name, package, version in (("openpyxl", "openpyxl", "3.1.5"), ("et-xmlfile", "et_xmlfile", "2.0.0")):
            wheel = self.cache/(package+".whl")
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(package+"/__init__.py", "# synthetic package\n")
                archive.writestr(package+"-"+version+".dist-info/METADATA", "Name: "+name+"\nVersion: "+version+"\n")
                archive.writestr(package+"-"+version+".dist-info/LICENSE", "synthetic license\n")
            self.assets.append({"name": name, "version": version, "filename": wheel.name,
                                "url": "https://invalid.example/never-used", **self.pack.info(wheel.read_bytes())})
        self.source = self.folder/"source.zip"
        files = {name: b"synthetic source" for name in self.source_pack.REQUIRED}
        doc = "WDMS_Cloud_Project/docs/moyu_portable.md"
        files[doc] = (self.root/doc).read_bytes()
        manifest = {"product": "摸鱼小工具", "source_commit": "a"*40, "windows_verified": False,
                    "files": {name: self.pack.info(data) for name, data in files.items()}}
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr("SOURCE_MANIFEST.json", json.dumps(manifest))
        self.asset_patch = patch.object(self.pack, "ASSETS", self.assets)
        self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)

    def make_runtime(self, *, missing=None, bits=0x8664, extra=None, link=False):
        with tarfile.open(self.runtime_archive, "w:gz", encoding="utf-8") as archive:
            for name in (*[n for n in CRITICAL if "site-packages" not in n], "LICENSE.txt"):
                if name == missing:
                    continue
                data = pe(bits) if name.endswith((".exe", ".dll", ".pyd")) else b"synthetic runtime resource\n"
                item = tarfile.TarInfo("python/"+name)
                item.size = len(data)
                archive.addfile(item, BytesIO(data))
            if extra:
                item = tarfile.TarInfo(extra)
                if link:
                    item.type, item.linkname = tarfile.SYMTYPE, "../outside"
                archive.addfile(item, BytesIO())
        asset = {"name": "cpython", "version": "3.12.14", "filename": self.runtime_archive.name,
                 "url": "https://invalid.example/never-used", **self.pack.info(self.runtime_archive.read_bytes())}
        if self.assets:
            self.assets[0] = asset
        else:
            self.assets.append(asset)

    def build(self):
        with patch.object(self.pack.urllib.request, "urlopen", side_effect=AssertionError("offline build must not download")):
            return self.pack.build(self.source, self.cache, self.folder/"output")

    def extract(self):
        result = self.build()
        destination = self.folder/"extracted"
        with zipfile.ZipFile(result["package"]) as archive:
            archive.extractall(destination)
        return destination/"WDMS_Cloud_Project", result

    def test_offline_build_contains_python_tk_libraries_licenses_and_preserved_source(self):
        result = self.build()
        manifest = self.pack.verify(result["package"])
        self.assertFalse(manifest["windows_verified"])
        self.assertFalse(manifest["windows_runtime_executed"])
        with zipfile.ZipFile(result["package"]) as archive:
            self.assertIn("启动摸鱼小工具.bat", archive.namelist())
            for name in CRITICAL:
                self.assertIn("WDMS_Cloud_Project/runtime/"+name, archive.namelist())
            self.assertIn("WDMS_Cloud_Project/runtime/LICENSE.txt", archive.namelist())
            self.assertIn("WDMS_Cloud_Project/runtime/Lib/site-packages/openpyxl-3.1.5.dist-info/LICENSE", archive.namelist())
        with self.assertRaises(FileExistsError):
            self.pack.build(self.source, self.cache, self.folder/"output")

    def test_missing_tcl_and_wrong_binary_architecture_are_rejected(self):
        for missing, bits in (("tcl/tcl8.6/init.tcl", 0x8664), (None, 0x14c)):
            with self.subTest(missing=missing, bits=bits):
                self.make_runtime(missing=missing, bits=bits)
                with self.assertRaises(ValueError):
                    self.pack.runtime_files(self.cache)

    def test_runtime_paths_and_symlinks_cannot_escape_the_distribution(self):
        for name, link in (("python/../escape", False), ("/absolute", False), ("python/link", True)):
            with self.subTest(name=name):
                self.make_runtime(extra=name, link=link)
                with self.assertRaises(ValueError):
                    self.pack.runtime_files(self.cache)

    def test_corrupt_cached_asset_is_preserved_and_never_replaced_by_a_download(self):
        self.runtime_archive.write_bytes(b"damaged cached archive")
        with patch.object(self.pack.urllib.request, "urlopen") as network, self.assertRaises(ValueError):
            self.pack.runtime_files(self.cache, download=True)
        network.assert_not_called()
        self.assertEqual(self.runtime_archive.read_bytes(), b"damaged cached archive")

    def test_extracted_critical_files_check_detects_missing_or_changed_resources(self):
        project, _ = self.extract()
        marker = json.loads((project/"PORTABLE_RUNTIME.json").read_text(encoding="utf-8"))
        verify_critical(project/"runtime", marker)
        (project/"runtime"/"tcl"/"tk8.6"/"tk.tcl").write_bytes(b"changed")
        with self.assertRaises(WDMSError) as caught:
            verify_critical(project/"runtime", marker)
        self.assertEqual(caught.exception.code, "MOYU_RUNTIME_INCOMPLETE")

    def test_incomplete_runtime_manifest_cannot_omit_tk_checks(self):
        project, _ = self.extract()
        marker = json.loads((project/"PORTABLE_RUNTIME.json").read_text(encoding="utf-8"))
        marker["critical"].pop("DLLs/_tkinter.pyd")
        with self.assertRaises(WDMSError):
            verify_critical(project/"runtime", marker)

    def test_mac_cannot_report_that_bundled_windows_runtime_ran_successfully(self):
        project, _ = self.extract()
        with patch("moyu_tools.portable.sys.platform", "darwin"), self.assertRaises(WDMSError) as caught:
            check(project)
        self.assertEqual(caught.exception.code, "MOYU_RUNTIME_PLATFORM")

    def test_archive_tampering_and_false_windows_verified_claims_are_rejected(self):
        result = self.build()
        for change in ("payload", "claim"):
            destination = self.folder/(change+".zip")
            with zipfile.ZipFile(result["package"]) as source, zipfile.ZipFile(destination, "w") as target:
                for item in source.infolist():
                    data = source.read(item)
                    if change == "payload" and item.filename.endswith("runtime/python.exe"):
                        data += b"changed"
                    if change == "claim" and item.filename == "PORTABLE_MANIFEST.json":
                        manifest = json.loads(data)
                        manifest["windows_verified"] = True
                        data = json.dumps(manifest).encode("utf-8")
                    target.writestr(item, data)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.pack.verify(destination)

    def test_launcher_uses_bundled_check_before_interpreter_picker_and_never_installs(self):
        script = (self.root/"WDMS_Cloud_Project"/"moyu_tools"/"launch.ps1").read_text(encoding="utf-8-sig")
        branch = script[script.index("$moyuMarker"):script.index("$python = Join-Path $project '.venv")]
        self.assertIn("check_moyu_runtime.py", branch)
        self.assertIn("& $python -I -B", branch)
        self.assertNotIn("OpenFileDialog", branch)
        self.assertNotIn("pip install", script)
        self.assertNotIn("Invoke-WebRequest", script)

    def test_pe_validation_does_not_accept_arbitrary_or_truncated_executables(self):
        self.assertEqual(self.pack.pe_machine(pe()), 0x8664)
        for data in (b"fake", b"MZ"+b"\x00"*62, pe()[:66]):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                self.pack.pe_machine(data)
