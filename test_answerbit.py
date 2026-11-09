import contextlib
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import answerbit
from archive import ArchiveError

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None


@unittest.skipIf(Workbook is None, "install requirements.txt to test XLSX integration")
class AnswerbitTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bank = Path(temp.name) / "bank.xlsx"
        self.output = Path(temp.name) / "live.sqlite"
        book = Workbook()
        book.remove(book.active)
        for title, code in (("美国", "US"), ("英国", "GB"), ("德国", "DE")):
            sheet = book.create_sheet(title)
            sheet.append(["分类名称", "编号", "问题内容"])
            sheet.append(["测试", "TEST-01-" + code, "Shared question?" if code != "DE" else "Frage?"])
        book.create_sheet("说明")
        book.save(self.bank)
        self.day = "2026-11-01"

    def records(self):
        return [
            dict(task_id="mock-1", query_str="Shared question?", tags=[{"tag_name": "英国"}],
                 platform="test-engine", language="en", zone="US", date=self.day, analysis={"state": 2}),
            dict(task_id="mock-2", query_str="Shared question?", platform="test-engine",
                 language="en", zone="US", date=self.day, analysis={"state": 2}),
            dict(task_id="mock-3", query_str="Frage?", platform="test-engine",
                 language="de", zone="US", date=self.day, analysis={"state": 1}),
        ]

    def test_refresh_matches_number_and_preserves_unknowns(self):
        calls = []
        records = self.records()

        def call(path, payload):
            self.assertEqual(payload["brand_id"], "kalodata-test")
            calls.append((path, payload))
            if path == answerbit.LIST:
                offset = (payload["page"] - 1) * payload["page_size"]
                return {"scores": records[offset:offset + payload["page_size"]], "total": len(records)}
            self.assertEqual(payload["task_id"], "mock-1")
            return dict(prompt="Shared question?", platform="test-engine", language="en",
                        date=self.day, llm_output="RAW_ANSWER_SENTINEL Kalodata.", analysis={"state": 2})

        with patch.object(answerbit, "PAGE_SIZE", 2):
            report = answerbit.refresh(self.bank, self.output, "kalodata-test", self.day, self.day, call)
        self.assertEqual([path for path, _ in calls], [answerbit.LIST, answerbit.LIST, answerbit.DETAIL])
        self.assertTrue(calls[0][1]["include_unanalyzed"])
        self.assertEqual(report["batch"]["question_version"], hashlib.sha256(self.bank.read_bytes()).hexdigest())
        self.assertIsNone(report["coverage"])
        self.assertEqual({issue["code"] for issue in report["issues"]},
                         {"citation_unknown", "ambiguous_question", "answer_unconfirmed"})
        british = next(row for row in report["groups"] if row["market"] == "GB")
        self.assertEqual(british["mention_rate"], 1)
        self.assertIsNone(british["official_citation_rate"])
        self.assertNotIn(b"RAW_ANSWER_SENTINEL", self.output.read_bytes())
        self.assertNotIn(b"Shared question?", self.output.read_bytes())
        with contextlib.closing(sqlite3.connect(self.output.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("SELECT question_id, zone FROM sample WHERE task_id = 'mock-1'").fetchone(),
                             ("TEST-01-GB", "US"))
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM sample")

    def test_unfinished_or_broken_refresh_preserves_previous_snapshot(self):
        self.output.write_bytes(b"previous")
        record = self.records()[2]
        for result, expected in (
            ({"scores": [], "total": 0}, "无 Answerbit 记录"),
            ({"scores": [record], "total": 2}, "重复任务 ID"),
            ({"scores": [record], "total": 1}, "没有可判定"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ArchiveError, expected):
                    answerbit.refresh(self.bank, self.output, "kalodata-test", self.day, self.day,
                                      lambda path, payload: result)
                self.assertEqual(self.output.read_bytes(), b"previous")

    def test_refuses_platform_mutation(self):
        with self.assertRaisesRegex(ArchiveError, "只读允许列表"):
            answerbit.request("/geo/prompt/run", {}, "SYNTHETIC_KEY")

    def test_japanese_spelling_variants_are_separate_and_both_count_once(self):
        from openpyxl import load_workbook
        with contextlib.closing(load_workbook(self.bank)) as book:
            sheet = book.create_sheet("日本")
            sheet.append(["分类名称", "编号", "问题内容"])
            sheet.append(["测试", "TEST-B005-JP", "Kalodataとは？"])
            sheet.append(["测试", "TEST-B005-JP-v2", "カロデータとは？"])
            book.save(self.bank)
        records = [dict(task_id=f"ja-{i}", query_str=prompt, platform="test-engine", language="ja",
                        date=self.day, analysis={"state": 2})
                   for i, prompt in enumerate(("Kalodataとは？", "カロデータとは？"))]

        def call(path, payload):
            if path == answerbit.LIST:
                return {"scores": records, "total": 2}
            row = next(r for r in records if r["task_id"] == payload["task_id"])
            body = "カロデータ（Kalodata）" if row["task_id"] == "ja-0" else "ｶﾛﾃﾞｰﾀ https://kalodata.com/"
            return dict(query=row["query_str"], platform=row["platform"], language="ja", date=self.day,
                        llm_output=body, links=[])

        report = answerbit.refresh(self.bank, self.output, "kalodata-test", self.day, self.day, call)
        groups = {row["prompt_brand_form"]: row for row in report["groups"]}
        self.assertEqual(set(groups), {"latin", "ja"})
        self.assertTrue(all(row["named_prompt"] == 1 and row["mentioned"] == 1 for row in groups.values()))
        self.assertEqual((groups["latin"]["mentioned_latin"], groups["latin"]["mentioned_ja"]), (1, 1))
        self.assertEqual((groups["ja"]["mentioned_latin"], groups["ja"]["mentioned_ja"]), (0, 1))

    def test_feishu_export_dimension_and_batch_diagnostics(self):
        changed = io.BytesIO()
        with ZipFile(self.bank) as source, ZipFile(changed, "w") as target:
            for name in source.namelist():
                data = source.read(name)
                target.writestr(name, data.replace(b'dimension ref="A1:C2"', b'dimension ref="A1:A1"')
                                if name.startswith("xl/worksheets/") else data)
        self.bank.write_bytes(changed.getvalue())
        self.assertEqual(len(answerbit.load_bank(self.bank)[1]), 3)

        from openpyxl import load_workbook
        with contextlib.closing(load_workbook(self.bank)) as book:
            sheet = book.create_sheet("日本")
            sheet.append(["分类名称", "编号", "问题内容"])
            sheet.append([None, "TEST-01-JP", "Question one?"])
            sheet.append(["测试", "TEST-02-JP", "Question two?"])
            sheet.append(["测试", "TEST-02-JP", "Question three?"])
            book.save(self.bank)
        with self.assertRaises(ArchiveError) as error:
            answerbit.load_bank(self.bank)
        self.assertIn("日本 第 2 行 TEST-01-JP 缺少有效分类", str(error.exception))
        self.assertIn("编号 TEST-02-JP 重复：日本 第 3 行、日本 第 4 行", str(error.exception))


if __name__ == "__main__":
    unittest.main()
