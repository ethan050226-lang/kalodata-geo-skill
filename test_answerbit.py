import contextlib
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
