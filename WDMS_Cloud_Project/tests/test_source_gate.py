"""Regression checks for the offline source gate and portable test boundaries."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import get_type_hints
from unittest import mock

from core.query_policy import NativeMessagePolicy, QueryPolicy
from errors import WDMSError
from core.collection_contracts import ProgressNotice
from scripts import check_source
from services import worker_commands
from services.workbook_table import write_inventory_table
import test_collection_contracts as contract_tests


class SourceGateTests(unittest.TestCase):
    def codes(self, source, filename='/repo/source.py'):
        return {finding.code for finding in check_source.check_text(source, filename)}

    def test_gate_detects_a_missing_global_from_a_moved_module(self):
        self.assertIn('UNDEFINED_GLOBAL', self.codes('def start():\n    return ROOT\n'))

    def test_unused_import_requires_use_or_explicit_export(self):
        self.assertIn('UNUSED_IMPORT', self.codes('import json\n'))
        self.assertEqual(self.codes('import json\n__all__=["json"]\n'), set())
        self.assertEqual(self.codes('from services import helper as helper\n'), set())

    def test_gate_requires_test_text_encoding_and_allows_binary_io(self):
        name='/repo/tests/example.py'
        self.assertIn('TEXT_ENCODING', self.codes('from pathlib import Path\nPath("a").write_text("货主")\n', name))
        self.assertEqual(self.codes('from pathlib import Path\nPath("a").write_text("货主", encoding="utf-8")\nPath("a").open("rb")\n', name),set())

    def test_gate_requires_named_native_message_timeout(self):
        self.assertIn('MESSAGE_TIMEOUT_POLICY', self.codes('def send(api):\n    api.SendMessageTimeoutW(1,0,0,0,35,500,None)\n'))

    def test_gate_requires_a_reason_for_suppression(self):
        self.assertIn('UNEXPLAINED_PASS', self.codes('try:\n    raise OSError()\nexcept OSError:\n    pass\n'))
        self.assertEqual(self.codes('try:\n    raise OSError()\nexcept OSError:\n    # Cleanup must preserve the original failure.\n    pass\n'),set())

    def test_syntax_errors_are_gate_failures(self):
        self.assertIn('SYNTAX', self.codes('def broken(:\n'))

    def test_message_policy_rejects_invalid_or_unbounded_timeouts(self):
        for value in (True, 0, -1, 5001, float('inf'), 1.5):
            with self.subTest(value=value), self.assertRaises(WDMSError):
                NativeMessagePolicy(login_label_ms=value)
        self.assertEqual(QueryPolicy().messages.login_label_ms,2500)

    def test_public_boundary_annotations_resolve_on_python39(self):
        for target in (ProgressNotice.update, worker_commands.worker_command, write_inventory_table):
            self.assertIn('return',get_type_hints(target))


class PortableFixtureTests(unittest.TestCase):
    def test_symlink_privilege_failure_is_an_explicit_skip(self):
        error=OSError('synthetic privilege required')
        error.winerror=1314
        case=contract_tests.CollectionContractTests('test_receipt_rejects_symlink')
        result=unittest.TestResult()
        with mock.patch.object(Path,'symlink_to',side_effect=error):
            case.run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.skipped),1)

    def test_without_tk_collection_checks_still_run_and_only_gui_checks_skip(self):
        root=Path(__file__).resolve().parents[1]
        code='''import builtins,sys,unittest
original=builtins.__import__
def blocked(name,*args,**kwargs):
    if name in ('tkinter','_tkinter') or name.startswith('tkinter.'):
        raise ModuleNotFoundError('synthetic no Tk',name=name)
    return original(name,*args,**kwargs)
builtins.__import__=blocked
import test_trial_isolation as tests
suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(tests.SyntheticNoticeIsolationTests),
                         unittest.defaultTestLoader.loadTestsFromTestCase(tests.DemoWorkflowRouteTests)])
result=unittest.TextTestRunner().run(suite)
assert result.wasSuccessful() and result.testsRun==4 and len(result.skipped)==2
'''
        environment=os.environ.copy()
        environment['PYTHONPATH']=str(root/'tests')+os.pathsep+str(root)
        result=subprocess.run([sys.executable,'-B','-c',code],cwd=root,env=environment,
                              capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
