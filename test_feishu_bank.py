import tempfile
from pathlib import Path
import unittest
import builtins
import json
from types import SimpleNamespace
from unittest.mock import patch
from feishu_bank import fetch_bank, content_signature
from answerbit import MARKETS
from archive import ArchiveError

class BankTest(unittest.TestCase):
    def test_missing_excel_component_names_agent_as_handler_before_any_read(self):
        import answerbit
        original = builtins.__import__
        def no_excel(name, *args, **kwargs):
            if name == 'openpyxl' or name.startswith('openpyxl.'):
                raise ImportError('synthetic missing component')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__',side_effect=no_excel), patch('feishu_bank.cli_read') as remote:
            for action in (lambda:answerbit.load_bank(Path('unused.xlsx')),
                           lambda:content_signature(Path('unused.xlsx')),
                           lambda:fetch_bank('unused',Path('unused.xlsx'),remote)):
                with self.assertRaisesRegex(ArchiveError,'请由 Agent.*无需用户提供组件'):
                    action()
            remote.assert_not_called()

    def test_read_permission_message_is_plain_and_preserves_admin_details(self):
        from feishu_bank import cli_read
        for scopes in (['sheets:spreadsheet:readonly'],[]):
            result=SimpleNamespace(returncode=1,stderr=json.dumps({'error':{'code':'denied','missing_scopes':scopes}}))
            with patch('feishu_bank.shutil.which',return_value='synthetic-cli'), \
                 patch('feishu_bank.subprocess.run',return_value=result), \
                 self.assertRaises(ArchiveError) as caught:
                cli_read(['sheets','+workbook-info'])
            message=str(caught.exception)
            self.assertIn('当前 Agent',message)
            if scopes:
                self.assertIn('先报告并等待用户决定',message)
                self.assertIn('管理员核对信息：sheets:spreadsheet:readonly',message)
            else:
                self.assertIn('不把所有失败都当成缺权限',message)

    def reader(self, changed=False, truncated=False):
        count=0
        def read(args):
            nonlocal count
            if args[1]=='+workbook-info':
                count+=1
                return {'token':'test','revision':2 if changed and count>1 else 1,'title':'test',
                        'sheets':[{'sheet_name':n,'sheet_id':m,'row_count':3,'column_count':3,'resource_type':'sheet'} for n,m in MARKETS.items()]}
            code=args[args.index('--sheet-id')+1]
            return {'revision':1,'has_more':truncated,'row_indices':[1,2,3],'col_count':3,
                    'annotated_csv':f' 编号 ,问题内容, 分类名称 \nCore-D001-{code},Which tool?,发现度\n , , \n'}
        return read
    def test_stable_and_complete(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bank.xlsx';a=fetch_bank('test',p,self.reader());b=fetch_bank('test',p,self.reader())
            self.assertEqual(a['question_count'],11);self.assertEqual(a['question_version'],b['question_version'])
    def test_failure_preserves_cache(self):
        for options in ({'changed':True},{'truncated':True}):
            with tempfile.TemporaryDirectory() as d:
                p=Path(d)/'bank.xlsx';p.write_bytes(b'previous')
                with self.assertRaises(ArchiveError):fetch_bank('test',p,self.reader(**options))
                self.assertEqual(p.read_bytes(),b'previous')

    def test_content_comparison_ignores_export_formatting_but_tracks_cell_changes(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'original.xlsx';fetch_bank('test',source,self.reader())
            original=content_signature(source)
            exported=Path(d)/'browser-export.xlsx'
            book=load_workbook(source);book['美国'].column_dimensions['A'].width=35;book.save(exported);book.close()
            self.assertEqual(content_signature(exported),original)
            book=load_workbook(exported);book['美国']['B2']='Changed question';book.save(exported);book.close()
            self.assertNotEqual(content_signature(exported),original)
