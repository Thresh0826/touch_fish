import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class MoyuPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        path = root/'工具'/'打包摸鱼小工具.py'
        spec = importlib.util.spec_from_file_location('moyu_packager_for_test', path)
        cls.packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.packager)

    def package(self, path, *, wrong_hash=False, unsafe=False, duplicate=False, verified=False):
        files = {name: b'synthetic source' for name in self.packager.REQUIRED}
        manifest = {'product': '摸鱼小工具', 'windows_verified': verified,
                    'files': {name: self.packager.summary(data) for name, data in files.items()}}
        if wrong_hash:
            next(iter(manifest['files'].values()))['sha256'] = '0'*64
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr('SOURCE_MANIFEST.json', json.dumps(manifest))
            if unsafe:
                archive.writestr('../escape.txt', 'synthetic')
            if duplicate:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(next(iter(files)), 'duplicate')

    def test_exact_member_size_hash_and_unverified_declaration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'package.zip'
            self.package(path)
            self.assertEqual(self.packager.verify(path)['product'], '摸鱼小工具')

    def test_corrupt_manifest_different_members_and_false_verified_claim_are_rejected(self):
        for change in ('wrong_hash', 'unsafe', 'duplicate', 'verified'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/'package.zip'
                self.package(path, **{change: True})
                with self.assertRaises(ValueError):
                    self.packager.verify(path)
