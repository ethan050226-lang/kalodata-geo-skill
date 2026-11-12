import tempfile
from pathlib import Path
import unittest
from feishu_bank import fetch_bank, content_signature
from answerbit import MARKETS
from archive import ArchiveError

class BankTest(unittest.TestCase):
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
