import json
import os
from pathlib import Path
import tempfile
import unittest
import io
from contextlib import redirect_stderr
from unittest.mock import patch
from answerbit import operator_credentials, question_bank_url
from archive import ArchiveError

class PrivateResourceTest(unittest.TestCase):
    def test_missing_brand_guides_agent_to_private_config_without_asking_for_variable(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'resources.json';path.write_text('{}')
            with patch.dict(os.environ,{'ANSWERBIT_API_KEY':'TEST_ONLY','ANSWERBIT_RESOURCE_CONFIG':str(path)},clear=True), \
                 self.assertRaises(ArchiveError) as caught:
                operator_credentials()
            message=str(caught.exception)
            self.assertIn('请由 Agent 先检查已保存的私有配置',message)
            self.assertNotIn('ANSWERBIT_BRAND_ID',message)
            self.assertNotIn('TEST_ONLY',message)

    def test_key_is_required_before_private_file_is_read(self):
        with patch.dict(os.environ,{},clear=True),patch('sys.stdin.isatty',return_value=False),patch.object(Path,'read_text') as read:
            with self.assertRaises(ArchiveError):operator_credentials()
        read.assert_not_called()
    def test_private_config_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'resources.json';path.write_text(json.dumps({'brand_id':'demo-brand','team_id':'demo-team'}))
            with patch.dict(os.environ,{'ANSWERBIT_API_KEY':'TEST_ONLY','ANSWERBIT_RESOURCE_CONFIG':str(path)},clear=True):
                self.assertEqual(operator_credentials(),('TEST_ONLY','demo-brand'))
                with patch.dict(os.environ,{'ANSWERBIT_BRAND_ID':'override-brand'}):
                    self.assertEqual(operator_credentials(),('TEST_ONLY','override-brand'))
    def test_invalid_private_config_does_not_echo_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'resources.json';path.write_text('PRIVATE_BAD_CONTENT')
            with patch.dict(os.environ,{'ANSWERBIT_API_KEY':'TEST_ONLY','ANSWERBIT_RESOURCE_CONFIG':str(path)},clear=True):
                with self.assertRaises(ArchiveError) as caught:operator_credentials()
                self.assertNotIn('PRIVATE_BAD_CONTENT',str(caught.exception))

    def test_bank_configuration_is_gated_and_private_then_environment_override(self):
        with patch.object(Path,'read_text') as read:
            with self.assertRaises(ArchiveError):question_bank_url(None)
        read.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'resources.json'
            private_url='https://example.feishu.cn/wiki/synthetic-bank?sheet=demo'
            path.write_text(json.dumps({'brand_id':'demo-brand','team_id':'demo-team','question_bank_url':private_url}))
            with patch.dict(os.environ,{'ANSWERBIT_API_KEY':'TEST_ONLY','ANSWERBIT_RESOURCE_CONFIG':str(path)},clear=True):
                key,brand=operator_credentials()
                self.assertEqual(brand,'demo-brand')
                self.assertEqual(question_bank_url(key),private_url)
                with patch.dict(os.environ,{'GEO_QUESTION_BANK_URL':'https://example.feishu.cn/sheets/override'}):
                    self.assertTrue(question_bank_url(key).endswith('/override'))
                path.write_text(json.dumps({'question_bank_url':'https://private-should-not-be-echoed.test/bank'}))
                with self.assertRaises(ArchiveError) as error:question_bank_url(key)
                self.assertNotIn('private-should-not-be-echoed',str(error.exception))

    def test_metrics_cli_also_stops_before_answerbit_without_live_bank(self):
        import answerbit
        with patch('sys.argv',['answerbit.py','refresh','bank.xlsx','snapshot.sqlite','--begin','2026-11-01','--end','2026-11-01']), \
             patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')), \
             patch.object(answerbit,'question_bank_url',return_value=None), \
             patch.object(answerbit,'refresh') as pull,redirect_stderr(io.StringIO()):
            self.assertEqual(answerbit.main(),1)
        pull.assert_not_called()
