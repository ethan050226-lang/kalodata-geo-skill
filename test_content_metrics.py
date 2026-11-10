import unittest
from collections import defaultdict

from answerbit import content_flags
from content_metrics import summarize_content


class ContentMetricsTest(unittest.TestCase):
    def test_competitor_order_echo_url_and_repeat(self):
        question = "Is Kalodata useful?"
        answer = "> **Question: Is Kalodata useful?**\n# FastMoss\n| Winning Hunter | Kalodata |\nKalodata https://echotik.example/"
        _, ranks, _, _, _ = content_flags(answer, question, "US", None)
        self.assertEqual(ranks, {"FastMoss": 1, "Winning Hunter": 2, "Kalodata": 3})
        _, ranks, _, _, _ = content_flags(answer, question, "GB", None)
        self.assertEqual(ranks, {"FastMoss": 1, "Kalodata": 2})
        _, ranks, _, _, _ = content_flags("Astream Deep、出海匠、ｶﾛﾃﾞｰﾀ。", question, "JP", None)
        self.assertEqual(ranks, {"Astream Deep": 1, "Kalodata": 2})
        _, ranks, _, _, _ = content_flags("出海匠、Kalodata。", question, "KR", None)
        self.assertEqual(ranks, {"出海匠": 1, "Kalodata": 2})

    def test_recommendations_are_exclusive(self):
        cases = {
            "I recommend Kalodata for mature teams. If you track competitors weekly, I recommend Kalodata.": "neutral",
            "I recommend Kalodata.": "explicit",
            "If you track competitors weekly, use Kalodata? I recommend Kalodata only for teams with weekly tracking.": "conditional",
            "I do not recommend Kalodata.": "no_recommendation",
            "I recommend FastMoss or Kalodata.": "unknown",
            "Users say they recommend Kalodata.": "unknown",
            "只在需要竞品跟踪时推荐 Kalodata。": "conditional",
            "推荐成熟团队使用 Kalodata；如果预算有限，只有每周跟踪竞品才推荐 Kalodata。": "neutral",
            "Kalodata is listed in the comparison.": "unknown",
            "EchoTik appears here.": "no_recommendation",
        }
        for answer, expected in cases.items():
            with self.subTest(answer=answer):
                self.assertEqual(content_flags(answer, "Which tool?", "US", None)[2], expected)

    def test_aggregate_by_market_and_retain_neutral_references(self):
        base = dict(market="US", named_prompt=0, platform="demo", group_id="g", group_name="Demo",
                    question_id="demo-US", task_date="2026-11-09", prompt="Which tool?", sentiment="unknown")
        rows = [
            dict(base, task_id="one", ranks={"FastMoss": 1, "Kalodata": 2}, recommendation="neutral", reputation_code=4),
            dict(base, task_id="two", ranks={"Kalodata": 1}, recommendation="explicit", reputation_code=None),
            dict(base, task_id="three", ranks={}, recommendation="no_recommendation", reputation_code=None),
        ]
        tags = defaultdict(list, {"one": ["x", "y"], "two": ["y"]})
        result = summarize_content(rows, tags)
        market = result["dimensions"]["market"][0]
        self.assertEqual((market["valid"], market["sov_denominator"], market["first"], market["rank_count"]), (3, 3, 1, 2))
        self.assertAlmostEqual(market["brands"][0]["sov"], 2 / 3)
        self.assertEqual(market["average_rank"], 1.5)
        self.assertEqual([(r["value"], r["valid"]) for r in result["dimensions"]["tag"]], [("x", 1), ("y", 2)])
        self.assertEqual(result["recommendation"]["neutral"], 1)
        self.assertEqual(result["neutral_questions"][0]["answers"][0]["prompt"], "Which tool?")
        self.assertEqual(result["sentiment"]["neutral"], 1)
        self.assertEqual(result["sentiment"]["unknown"], 2)

    def test_platform_reputation_codes_and_missing_values(self):
        cases = [(0, "not_mentioned"), (1, "positive"), (2, "negative"), (3, "mixed"), (4, "neutral"),
                 (None, "unknown"), (9, "unknown"), (True, "unknown"), ("1", "unknown")]
        for code, label in cases:
            with self.subTest(code=code):
                self.assertEqual(content_flags("Kalodata", "Which tool?", "US", code)[-1], label)
