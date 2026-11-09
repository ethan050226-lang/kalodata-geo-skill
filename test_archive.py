import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import archive


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.json"
        self.draft = self.root / "draft.sqlite"
        self.shared = self.root / "shared"
        self.local = self.root / "local.sqlite"
        self.fixture = json.loads(Path(__file__).with_name("examples").joinpath("demo-batch.json").read_text())
        self.source.write_text(json.dumps(self.fixture))

    def test_round_trip_counts_privacy_and_repeated_sync(self):
        self.fixture["batch"]["api_key"] = "SYNTHETIC_SECRET_SENTINEL"
        self.source.write_text(json.dumps(self.fixture))
        preview = archive.prepare(self.source, self.draft)
        totals = preview["totals"]
        self.assertEqual((totals["planned"], totals["valid"], totals["missing"], totals["failed"]), (5, 3, 1, 1))
        self.assertEqual(
            (totals["coverage"], totals["mention_rate"], totals["official_citation_rate"],
             totals["citation_field_coverage"]), (3 / 5, 2 / 3, 1 / 2, 2 / 3)
        )
        self.assertEqual({issue["code"] for issue in preview["issues"]},
                         {"duplicate", "citation_unknown", "failed", "missing"})
        self.assertEqual(len(preview["groups"]), 2)
        self.assertNotIn("SYNTHETIC_ANSWER_SENTINEL", json.dumps(preview))
        self.assertNotIn(b"SYNTHETIC_ANSWER_SENTINEL", self.draft.read_bytes())
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", json.dumps(preview))
        self.assertNotIn(b"SYNTHETIC_SECRET_SENTINEL", self.draft.read_bytes())
        self.assertNotIn(b"Which tools help", self.draft.read_bytes())
        first = archive.publish(self.draft, self.shared)
        self.assertEqual(archive.publish(self.draft, self.shared), first)
        self.assertEqual(archive.sync(self.shared, self.local), first)
        self.assertEqual(archive.sync(self.shared, self.local), first)
        self.assertEqual(archive.inspect(self.local), preview)
        with contextlib.closing(sqlite3.connect(self.local.as_uri() + "?mode=ro", uri=True)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM sample")

    def test_bad_inputs_do_not_replace_latest(self):
        archive.prepare(self.source, self.draft)
        archive.publish(self.draft, self.shared)
        before = (self.shared / archive.INDEX).read_bytes()
        cases = (
            ("retrieval", lambda data: data["batch"].update(retrieval_complete=False), "未取全"),
            ("denominator", lambda data: data.update(plan=[]), "分母未知"),
            ("question", lambda data: data["samples"][0].update(question="mismatch"), "题文冲突"),
            ("duplicate", lambda data: data["samples"][1].update(answer="conflict"), "内容不一致"),
            ("real", lambda data: data["batch"].update(synthetic=False), "synthetic"),
        )
        for name, change, expected in cases:
            with self.subTest(name=name):
                data = deepcopy(self.fixture)
                change(data)
                self.source.write_text(json.dumps(data))
                broken_draft = self.root / f"{name}.sqlite"
                with self.assertRaisesRegex(archive.ArchiveError, expected):
                    archive.prepare(self.source, broken_draft)
                self.assertFalse(broken_draft.exists())
                self.assertEqual((self.shared / archive.INDEX).read_bytes(), before)

    def test_bad_sync_reports_preserved_batch(self):
        archive.prepare(self.source, self.draft)
        archive.publish(self.draft, self.shared)
        archive.sync(self.shared, self.local)
        before = self.local.read_bytes()
        self.fixture["batch"]["id"] = "trial-002"
        self.source.write_text(json.dumps(self.fixture))
        archive.prepare(self.source, self.draft)
        newer = archive.publish(self.draft, self.shared)
        (self.shared / newer["file"]).write_bytes(b"corrupt")
        output = io.StringIO()
        with patch("sys.argv", ["archive.py", "sync", str(self.shared), str(self.local)]):
            with contextlib.redirect_stderr(output):
                self.assertEqual(archive.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["retained_batch"]["id"], "trial-001")
        self.assertEqual(self.local.read_bytes(), before)

    def test_unknown_sources_and_empty_denominator(self):
        self.fixture["samples"][0]["citations"] = ["https://kalodata.com.example/"]
        self.fixture["samples"][1]["citations"] = ["https://kalodata.com.example/"]
        self.fixture["samples"][2]["citations"] = ["invalid"]
        self.source.write_text(json.dumps(self.fixture))
        preview = archive.prepare(self.source, self.draft)
        self.assertEqual(preview["totals"]["official"], 0)
        german = next(row for row in preview["groups"] if row["market"] == "DE")
        self.assertIsNone(german["official_citation_rate"])
        self.assertEqual(german["citation_field_coverage"], 0)


if __name__ == "__main__":
    unittest.main()
