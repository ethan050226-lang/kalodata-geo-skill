"""Render the enhanced, restricted dashboard from a complete prepared GEO batch."""
import argparse
from collections import Counter
import json
from pathlib import Path

from dashboard import retained_data
from answerbit import write_atomic

ASSET = Path(__file__).resolve().parent / "skill/geo-sampling/assets/enhanced-dashboard.html"


def cohort(dimension):
    return (*dimension[:5], tuple(sorted(dimension[5])))


def render(data):
    details = data["answer_details"]
    records = details["records"]
    originals = {record["task"]: record for record in records}
    if not records or len(originals) != len(records) or len(records) != details["record_count"]:
        raise ValueError("回答原文明细缺失或任务重复")
    counts = Counter(cohort(record["d"]) for record in records)
    expected = {cohort(row["d"]): row["n"] for row in data["rows"]}
    if counts != expected or any(not isinstance(r["body"], str) or not r["body"].strip() for r in records):
        raise ValueError("回答原文与看板指标样本未对齐")
    for item in (data.get("quality") or {}).get("records", []):
        if item["status"] == "accepted" and item["task"] not in originals:
            raise ValueError("长度预警无法定位已纳入的原文")
    for field in ("source_batch", "platform_snapshot", "failed_attempt", "previous_candidate"):
        if field not in data["preview"]:
            raise ValueError(f"缺少预览证据字段 {field}")
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    template = ASSET.read_text(encoding="utf-8")
    if template.count("__GEO_PAYLOAD__") != 1:
        raise ValueError("模板须有唯一的数据占位符")
    return template.replace("__GEO_PAYLOAD__", payload, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Private prepared JSON payload or enriched HTML snapshot")
    parser.add_argument("output", type=Path, help="Local output HTML populated with the supplied originals")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("请使用不同的输入和输出路径")
    try:
        data = (json.loads(args.source.read_text(encoding="utf-8")) if args.source.suffix.lower() == ".json"
                else retained_data(args.source)[1])
        write_atomic(args.output, render(data).encode("utf-8"))
    except (KeyError, TypeError, ValueError, OSError) as error:
        parser.exit(1, f"增强看板生成失败：{error}\n")


if __name__ == "__main__":
    main()
