import tempfile
import unittest
from pathlib import Path
from unittest import mock
from desktop_demo.settings import save_settings, load_settings
from scripts.run_native_trial import trial_command

class DemoSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.exe=self.root/'App.exe';self.exe.write_bytes(b'demo fixture')
        self.output=self.root/'reports'
        self.path=self.root/'settings.json'
        self.credentials=mock.Mock()
        self.credentials.exists.return_value=True

    def save(self,password='test-password',previous=None,username='test-user'):
        return save_settings(str(self.exe),username,password,str(self.output),self.credentials,previous,path=self.path)

    def test_password_never_enters_configuration(self):
        settings=self.save()
        self.assertEqual(load_settings(self.path),settings)
        data=self.path.read_text(encoding='utf-8')
        self.assertNotIn('test-password',data)
        self.assertNotIn('"password"',data)
        self.credentials.write.assert_called_once_with(settings.credential_target,'test-user','test-password')

    def test_settings_edit_can_preserve_existing_password(self):
        initial=self.save();self.credentials.write.reset_mock()
        revised=self.save(password='',previous=initial)
        self.assertEqual(initial.credential_target,revised.credential_target)
        self.credentials.write.assert_not_called()

    def test_changing_account_requires_password(self):
        initial=self.save()
        with self.assertRaises(ValueError):self.save(password='',previous=initial,username='another-user')
        self.assertEqual(load_settings(self.path).username,'test-user')

    def test_missing_credential_cannot_be_silently_preserved(self):
        initial=self.save();self.credentials.exists.return_value=False
        with self.assertRaises(ValueError):self.save(password='',previous=initial)

    def test_invalid_executable_writes_no_credentials_or_config(self):
        with self.assertRaises(ValueError):
            save_settings(str(self.root/'missing.exe'),'user','test-password',str(self.output),self.credentials,path=self.path)
        self.credentials.write.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_credential_error_has_no_plaintext_fallback(self):
        self.credentials.write.side_effect=ValueError('credential storage unavailable')
        with self.assertRaises(ValueError):self.save()
        self.assertFalse(self.path.exists())

    def test_invalid_configuration_reports_reconfiguration(self):
        self.path.write_text('{"username":123}', encoding='utf-8')
        with self.assertRaises(ValueError):load_settings(self.path)

    def test_frozen_worker_command_routes_without_script_path(self):
        import sys
        with mock.patch.object(sys,'frozen',True,create=True):
            command=trial_command(['--_child','--index','2'])
        self.assertEqual(command,[sys.executable,'--native-trial','--_child','--index','2'])


if __name__=='__main__':unittest.main()
