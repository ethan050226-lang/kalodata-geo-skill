import contextlib
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import warnings
from unittest.mock import patch
from zipfile import ZipFile

import answerbit
import dashboard
from archive import ArchiveError

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None


class OperatorCredentialsTest(unittest.TestCase):
    def test_missing_key_blocks_noninteractive_refresh(self):
        args = ["answerbit.py", "refresh", "bank.xlsx", "local.sqlite", "--begin", "2026-11-01",
                "--end", "2026-11-01"]
        error = io.StringIO()
        with patch.dict(os.environ, {"ANSWERBIT_BRAND_ID": "demo-brand"}, clear=True), \
                patch("sys.argv", args), patch("sys.stdin.isatty", return_value=False), \
                patch.object(answerbit, "refresh") as refresh, contextlib.redirect_stderr(error):
            self.assertEqual(answerbit.main(), 1)
        refresh.assert_not_called()
        self.assertIn("当前运行者", error.getvalue())

    def test_operator_environment_or_hidden_prompt(self):
        with patch.dict(os.environ, {"ANSWERBIT_BRAND_ID": "demo-brand", "ANSWERBIT_API_KEY": "TEST_ONLY"},
                        clear=True), patch.object(answerbit, "getpass") as prompt:
            self.assertEqual(answerbit.operator_credentials(), ("TEST_ONLY", "demo-brand"))
        prompt.assert_not_called()
        with patch.dict(os.environ, {"ANSWERBIT_BRAND_ID": "demo-brand"}, clear=True), \
                patch("sys.stdin.isatty", return_value=True), \
                patch.object(answerbit, "getpass", return_value="TEST_ONLY") as prompt:
            self.assertEqual(answerbit.operator_credentials(), ("TEST_ONLY", "demo-brand"))
        prompt.assert_called_once()

    def test_hidden_input_fails_closed_if_terminal_would_echo(self):
        def unsafe_prompt(_):
            warnings.warn("terminal echo fallback", answerbit.GetPassWarning)
            raise AssertionError("must stop before reading the key")

        with patch.dict(os.environ, {"ANSWERBIT_BRAND_ID": "demo-brand"}, clear=True), \
                patch("sys.stdin.isatty", return_value=True), patch.object(answerbit, "getpass", unsafe_prompt):
            with self.assertRaisesRegex(ArchiveError, "无法安全获取"):
                answerbit.operator_credentials()


class WebsiteFlagsTest(unittest.TestCase):
    def test_body_and_formal_sources_are_independent(self):
        self.assertEqual(answerbit.website_flags(
            "访问kalodata.com。也可看 [FastMoss](https://www.fastmoss.com/)，同一链接 https://kalodata.com/",
            ["https://docs.kalodata.com/"],
        ), [("Kalodata", 1, 1), ("FastMoss", 1, 0)])
        self.assertEqual(answerbit.website_flags(
            "https://example.com/kalodata.com https://kalodata.com.evil.test notfastmoss.com "
            "info@kalodata.com example.com?next=fastmoss.com https://kalodata.com@evil.test",
            None,
        ), [("Kalodata", 0, None), ("FastMoss", 0, None)])


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
            dict(task_id="mock-1", query_str="Shared question?", title_id="group-research", title_name="商品调研",
                 tags=[{"tag_id": "gb", "tag_name": "英国", "tag_type": 1},
                       {"tag_id": "core", "tag_name": "Core", "tag_type": 2},
                       {"tag_id": "core", "tag_name": "Core", "tag_type": 2},
                       {"tag_id": "model", "tag_name": "测试模型", "tag_type": 3}],
                 platform="test-engine", language="en", zone="US", date=self.day, analysis={"state": 2}),
            dict(task_id="mock-2", query_str="Shared question?", platform="test-engine",
                 language="en", zone="US", date=self.day, analysis={"state": 2}),
            dict(task_id="mock-3", query_str="Frage?", tags=[], platform="test-engine",
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
            return dict(prompt="Shared question?", platform="test-engine", language="en", date=self.day,
                        llm_output="RAW_ANSWER_SENTINEL Kalodata. See fastmoss.com.", brand_reputation=4,
                        analysis={"state": 2})

        with patch.object(answerbit, "PAGE_SIZE", 2):
            report = answerbit.refresh(self.bank, self.output, "kalodata-test", self.day, self.day, call)
        self.assertEqual([path for path, _ in calls], [answerbit.LIST, answerbit.LIST, answerbit.DETAIL])
        self.assertTrue(calls[0][1]["include_unanalyzed"])
        self.assertTrue(all(payload["mention_brand"] == -1 for path, payload in calls if path == answerbit.LIST))
        self.assertEqual(report["batch"]["question_version"], hashlib.sha256(self.bank.read_bytes()).hexdigest())
        self.assertIsNone(report["coverage"])
        self.assertEqual({issue["code"] for issue in report["issues"]},
                         {"citation_unknown", "ambiguous_question", "answer_unconfirmed"})
        british = next(row for row in report["groups"] if row["market"] == "GB")
        self.assertEqual(british["mention_rate"], 1)
        self.assertIsNone(british["official_citation_rate"])
        self.assertEqual(sum(row["matched"] for row in report["groups"]), 2)
        self.assertEqual({(row["tag_id"], row["tag_type"], row["matched"]) for row in report["answerbit_tags"]},
                         {("gb", 1, 1), ("core", 2, 1), ("model", 3, 1)})
        self.assertEqual(next(row for row in report["answerbit_groups"] if row["market"] == "GB")["group_name"],
                         "商品调研")
        self.assertEqual(len(answerbit.read_snapshot(self.output, "group-research", ["gb", "core"])["groups"]), 1)
        self.assertEqual(answerbit.read_snapshot(self.output, "group-research", ["gb", "missing"])["groups"], [])
        content = answerbit.read_snapshot(self.output, "group-research", ["gb", "core"])["content"]
        self.assertEqual(content["valid"], 1)
        self.assertEqual(content["sentiment_raw_counts"], {"4": 1})
        self.assertEqual(content["sentiment"]["neutral"], 1)
        self.assertEqual(content["sentiment"]["unknown"], 0)
        self.assertEqual(content["dimensions"]["market"][0]["first_rate"], 1)
        self.assertEqual(content["dimensions"]["market"][0]["sov_denominator"], 1)
        self.assertEqual(answerbit.read_snapshot(self.output, "group-research", ["gb", "missing"])["content"]["valid"], 0)
        links = {row["brand"]: row for row in answerbit.read_snapshot(self.output, "group-research", ["core"])["website_links"]}
        self.assertEqual(links["FastMoss"]["body_link_rate"], 1)
        self.assertEqual(links["Kalodata"]["body_link_rate"], 0)
        self.assertIsNone(links["FastMoss"]["formal_link_rate"])
        self.assertEqual(links["FastMoss"]["formal_field_coverage"], 0)
        self.assertEqual(answerbit.read_snapshot(self.output, "other-group")["website_links"], [])
        self.assertEqual({row["group_id"] for row in report["website_links_by_group"]}, {"group-research"})
        self.assertEqual({row["tag_id"] for row in report["website_links_by_tag"]}, {"gb", "core", "model"})
        self.assertTrue(all(row["valid"] == 1 for row in report["website_links_by_tag"]))
        self.assertNotIn(b"RAW_ANSWER_SENTINEL", self.output.read_bytes())
        self.assertNotIn(b"Shared question?", self.output.read_bytes())
        with contextlib.closing(sqlite3.connect(self.output.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("SELECT question_id, zone FROM sample WHERE task_id = 'mock-1'").fetchone(),
                             ("TEST-01-GB", "US"))
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM sample")

    def test_neutral_snapshot_retains_prompt_but_not_answer_and_reads_old_version(self):
        record = self.records()[0]

        def call(path, payload):
            if path == answerbit.LIST:
                return {"scores": [record], "total": 1}
            return dict(prompt=record["query_str"], platform=record["platform"],
                        language=record["language"], date=self.day, links=[],
                        llm_output="I recommend Kalodata. If you track weekly, I recommend Kalodata. PRIVATE_BODY")

        report = answerbit.refresh(self.bank, self.output, "kalodata-test", self.day, self.day, call)
        content = report["content"]
        self.assertEqual(content["recommendation"]["neutral"], 1)
        self.assertEqual(content["recommendation"]["explicit"], 0)
        self.assertEqual(content["neutral_questions"][0]["answers"][0]["prompt"], record["query_str"])
        self.assertNotIn(b"PRIVATE_BODY", self.output.read_bytes())
        with contextlib.closing(sqlite3.connect(self.output)) as db, db:
            db.execute("DROP TABLE sample_content")
            db.execute("UPDATE batch SET metric_version = ?", ("answerbit-observed/2",))
        old = answerbit.read_snapshot(self.output)
        self.assertEqual(old["content_status"], "需刷新")
        self.assertEqual(old["groups"][0]["mentioned_latin"], 1)
        self.assertIsNotNone(old["website_links"])

    def test_dashboard_refresh_counts_and_failed_update_preserves_batch(self):
        record = self.records()[0]

        def call(path, payload):
            if path == answerbit.LIST:
                return {"scores":[record], "total":1}
            return dict(prompt=record["query_str"], platform=record["platform"],
                        language=record["language"], date=self.day, links=[], brand_reputation=1,
                        llm_output="Kalodata compared with FastMoss. PRIVATE_ANSWER")

        dashboard.refresh_answers(self.bank,self.output,"test",self.day,self.day,call)
        data = dashboard.dashboard_data(self.output,self.bank,None)
        self.assertEqual(sum(row["n"] for row in data["rows"]),1)
        self.assertEqual(data["rows"][0]["d"][-1],("core","gb","model"))
        self.assertEqual(data["rows"][0]["mentions"],{"Kalodata":1,"FastMoss":1})
        self.assertEqual(data["rows"][0]["sent"],{"positive":1})
        self.assertEqual(data["rows"][0]["links"]["Kalodata"],[0,1,0,1])
        target = self.output.with_suffix(".html")
        dashboard.render(data,target)
        self.assertNotIn("PRIVATE_ANSWER",target.read_text())
        self.assertNotIn("</script>",dashboard.encode({"unsafe":"</script>"}))
        with patch("sys.argv",["dashboard.py","snapshot",str(self.bank),str(self.output.with_name("missing.sqlite")),str(target)]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dashboard.main(),1)
        _, retained, _ = dashboard.retained_data(target)
        self.assertEqual(retained["batch"],data["batch"])
        self.assertEqual(retained["last_failure"]["last_success"],data["batch"]["fetched_at_utc"])

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
        records = [dict(task_id=f"ja-{i}", query_str=prompt, platform="test-engine", language="ja", tags=[],
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
