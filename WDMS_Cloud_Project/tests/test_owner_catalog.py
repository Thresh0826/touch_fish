import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from errors import WDMSError
from services import owner_catalog as catalog


class OwnerCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = SimpleNamespace(wdms_exe=str(self.root/'Synthetic.exe'),
                                      username='synthetic-user', credential_target='synthetic-target')
        self.names = [f'SyntheticOwner{i:02d}' for i in range(24)]
        self.serial = 0

    def snapshot(self, names=None):
        self.serial += 1
        path = self.root/f'snapshot_{self.serial}.json'
        catalog.write_snapshot(path, self.names if names is None else names,
                               self.config, {'pid':7, 'hwnd':100})
        return path

    def test_first_list_needs_no_interaction_or_persistent_confirmation(self):
        path = self.snapshot()
        gui = SimpleNamespace(Tk=mock.Mock(side_effect=AssertionError('Unexpected owner dialog')))
        with mock.patch.dict('sys.modules', {'tkinter':gui}):
            names, digest = catalog.prepare_collection(path, self.config)
        self.assertEqual(names, tuple(self.names))
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        gui.Tk.assert_not_called()
        self.assertFalse((self.root/'confirmed_owners.json').exists())

    def test_changed_names_and_order_are_used_without_waiting_for_confirmation(self):
        catalog.prepare_collection(self.snapshot(), self.config)
        changed = list(reversed(self.names)); changed[0] = 'ChangedSyntheticOwner'
        names, digest = catalog.prepare_collection(self.snapshot(changed), self.config)
        self.assertEqual(names, tuple(changed))

    def test_old_confirmation_cache_does_not_block_collection_or_get_overwritten(self):
        old = self.root/'confirmed_owners.json'; old.write_text('{broken', encoding='utf-8')
        catalog.prepare_collection(self.snapshot(), self.config)
        self.assertEqual(old.read_text(encoding='utf-8'), '{broken')

    def test_config_context_change_after_capture_still_stops_collection(self):
        path = self.snapshot()
        for field, value in (('username','other-user'), ('wdms_exe',str(self.root/'Other.exe')),
                             ('credential_target','other-target')):
            config = SimpleNamespace(**vars(self.config)); setattr(config, field, value)
            with self.subTest(field=field):
                with self.assertRaises(WDMSError) as caught:
                    catalog.prepare_collection(path, config)
                self.assertEqual(caught.exception.code, 'OWNER_SESSION_CHANGED')

    def test_cancel_before_or_after_validation_still_stops_collection(self):
        for cancel in (mock.Mock(return_value=True), mock.Mock(side_effect=[False,True])):
            with self.assertRaises(WDMSError) as caught:
                catalog.prepare_collection(self.snapshot(), self.config, cancelled=cancel)
            self.assertEqual(caught.exception.code, 'TASK_CANCELLED')

    def test_snapshot_change_between_reads_is_rejected(self):
        path = self.snapshot(); key = catalog.context_key(self.config)
        def change(config):
            data = json.loads(path.read_text(encoding='utf-8')); data['owners'].reverse()
            data['fingerprint'] = catalog.fingerprint(data['owners'])
            path.write_text(json.dumps(data), encoding='utf-8')
            return key
        with mock.patch.object(catalog,'context_key',side_effect=change):
            with self.assertRaises(WDMSError) as caught:
                catalog.prepare_collection(path,self.config)
        self.assertEqual(caught.exception.code,'OWNER_PLAN_CHANGED')

    def test_invalid_duplicate_empty_or_wrong_count_list_is_rejected(self):
        for names in (None, [], self.names[:3], self.names[:-1]+[self.names[0]],
                      self.names[:-1]+[' '], self.names[:-1]+[None]):
            with self.subTest(names=type(names).__name__):
                with self.assertRaises(WDMSError): catalog.validated_names(names)

    def test_snapshot_is_exclusive_and_fingerprint_metadata_is_checked(self):
        path = self.snapshot(); previous = path.read_bytes()
        with self.assertRaises(WDMSError):
            catalog.write_snapshot(path,self.names,self.config,{'pid':7,'hwnd':100})
        self.assertEqual(path.read_bytes(),previous)
        data=json.loads(previous); data['fingerprint']='incorrect'; path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(WDMSError): catalog.read_plan(path)

    def test_plain_legacy_file_is_not_an_automatic_capture_snapshot(self):
        path=self.root/'legacy.json'; path.write_text(json.dumps({'owners':self.names}), encoding='utf-8')
        self.assertEqual(catalog.read_plan(path)[1],tuple(self.names))
        with self.assertRaises(WDMSError) as caught: catalog.prepare_collection(path,self.config)
        self.assertEqual(caught.exception.code,'OWNER_SESSION_CHANGED')

    def test_invalid_session_identity_in_snapshot_is_rejected(self):
        path=self.snapshot(); data=json.loads(path.read_text(encoding='utf-8')); data['identity']['pid']=0
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(WDMSError) as caught: catalog.prepare_collection(path,self.config)
        self.assertEqual(caught.exception.code,'OWNER_PLAN_INVALID')


if __name__=='__main__': unittest.main()
