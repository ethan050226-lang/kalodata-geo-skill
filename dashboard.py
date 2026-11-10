"""Generate a self-contained, local GEO dashboard from Answerbit."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import islice
import json
import math
from pathlib import Path
import sqlite3
import sys
from threading import Lock
import time

import answerbit as api
from content_metrics import KALODATA, market_brands, reputation_label

CHINA = timezone(timedelta(hours=8))
ASSET = Path(__file__).resolve().parent / "skill/geo-sampling/assets/dashboard.html"
SOURCE = "https://answerbit.qq.com/zh-CN/dashboard/home"
QNA = "https://kalowave.feishu.cn/wiki/QhQewzO72ia8c8kxn8mcCwSDnOg?sheet=3Qrfdv"
DATA_MARKER = '<script id="geo-data" type="application/json">'


class PacedRead:
    def __init__(self, key):
        self.key = key
        self.lock = Lock()
        self.next_request = 0
        self.delay = .14

    def __call__(self, path, payload):
        for attempt in range(7):
            while True:
                with self.lock:
                    now = time.monotonic()
                    wait = self.next_request - now
                    if wait <= 0:
                        self.next_request = now + self.delay
                        break
                time.sleep(wait)
            try:
                return api.request(path, payload, self.key)
            except api.ArchiveError as error:
                if attempt == 6 or not any(x in str(error) for x in ("HTTP 429", "HTTP 502", "HTTP 503", "HTTP 504", "未获得可解析")):
                    raise
                with self.lock:
                    now = time.monotonic()
                    if self.next_request - now < 3:
                        self.next_request = now + 15
                        self.delay = min(.65, self.delay * 1.25)


def refresh_answers(bank, snapshot, brand, begin, end, call):
    # Fetch at most one small wave ahead. Answer bodies stay in memory.
    _, questions, by_text = api.load_bank(bank)
    listed, pending = [], {}
    remaining = iter(())
    with ThreadPoolExecutor(max_workers=5) as executor:
        def fetch(path, payload):
            nonlocal remaining
            if path == api.LIST:
                reply = call(path, payload)
                if payload["page"] == 1:
                    listed.clear()
                listed.extend(reply.get("scores", []))
                remaining = (row["task_id"] for row in listed if api.analysis_state(row) == 2
                             and api.select_question(row, questions, by_text)[0])
                return reply
            if not pending:
                for task in islice(remaining, 20):
                    pending[task] = executor.submit(call, path, {"brand_id": brand, "task_id": task})
            return pending.pop(payload["task_id"]).result()
        return api.refresh(bank, snapshot, brand, begin, end, fetch, page_size=500)


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def platform_snapshot(call, brand, begin, end):
    results, issues = {}, {}
    payload = dict(brand_id=brand, begin_date=begin, end_date=end)
    for path in api.PLATFORM_READS:
        try:
            data = call(path, {**payload, **({"page":1, "page_size":5} if path.endswith("/rank") else {})})
            if path.endswith("/dashboard"):
                values = {k: number(data[k].get("value")) for k in ("exposure","avg_rank","score")
                          if isinstance(data.get(k), dict)}
                values = {k:v for k,v in values.items() if v is not None}
            elif path.endswith("/trends"):
                field = "exposure" if "exposure" in path else "score"
                values = [{"date": r["date"], "value": number(r.get(field)), "count": number(r.get("task_count"))}
                          for r in data.get("brand_statistics", []) if isinstance(r, dict)
                          and isinstance(r.get("date"), str) and begin <= r["date"] <= end
                          and number(r.get(field)) is not None]
            else:
                field = "domain" if "/domain/" in path else "article"
                values = [{"label": r[field], "count": r["count"]} for r in data.get("reference_count", [])[:5]
                          if isinstance(r, dict) and isinstance(r.get(field), str)
                          and type(r.get("count")) is int and r["count"] >= 0]
            if values:
                results[path] = values
            else:
                issues[path] = "响应无可用数据"
        except (api.ArchiveError, TypeError, KeyError) as error:
            issues[path] = str(error) if isinstance(error, api.ArchiveError) else "响应结构待核对"
    return {"fetched": datetime.now(CHINA).isoformat(timespec="seconds"), "begin":begin, "end":end,
            "data":results, "issues":issues}


def dashboard_data(snapshot, bank, native):
    sha, _, by_text = api.load_bank(bank)
    prompts = {qid: text for text, ids in by_text.items() for qid in ids}
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        batch = dict(db.execute("SELECT * FROM batch").fetchone())
        api.require(batch["question_version"] == sha, "题库校验 / 快照和题库版本不一致")
        api.require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "快照校验 / SQLite 文件校验失败")
        tags = defaultdict(list)
        tag_names = {}
        for row in db.execute("SELECT task_id,tag_id,tag_name FROM sample_tag ORDER BY tag_id"):
            tags[row["task_id"]].append(row["tag_id"])
            tag_names[row["tag_id"]] = row["tag_name"]
        sites = defaultdict(dict)
        for row in db.execute("SELECT * FROM sample_website"):
            sites[row["task_id"]][row["brand"]] = [row["body_hit"], row["formal_hit"]]
        buckets = {}
        groups = {}
        neutral = []
        valid = 0
        valid_tasks = set()
        for row in db.execute("""
            SELECT s.task_id,s.question_id,s.platform,s.task_date,q.market,q.named_prompt,g.group_id,g.group_name,
                   c.ranks,c.recommendation,c.reputation_code
            FROM sample s JOIN question q USING(question_id) LEFT JOIN sample_group g USING(task_id)
            JOIN sample_content c USING(task_id) WHERE s.status='valid' ORDER BY s.task_id
        """):
            valid += 1
            task = row["task_id"]
            valid_tasks.add(task)
            market, platform, day, group, named = (row[k] for k in ("market","platform","task_date","group_id","named_prompt"))
            group = group or ""
            groups[group] = row["group_name"] or "分组未知"
            dim = (market, platform, day, group, named, tuple(tags[task]))
            if dim not in buckets:
                buckets[dim] = {"d": dim, "n":0, "mentions":Counter(), "first":0, "rank":[0,0],
                                "links":{}, "rec":Counter(), "sent":Counter()}
            b = buckets[dim]
            ranks = json.loads(row["ranks"])
            api.require(set(ranks) <= set(market_brands(market)) and
                        sorted(ranks.values()) == list(range(1,len(ranks)+1)),
                        "快照校验 / 品牌名次或市场竞品范围异常")
            b["n"] += 1
            b["mentions"].update(ranks.keys())
            rank = ranks.get(KALODATA)
            if rank:
                b["first"] += rank == 1
                b["rank"][0] += rank
                b["rank"][1] += 1
            for brand, (body, formal) in sites[task].items():
                v = b["links"].setdefault(brand,[0,0,0,0])
                v[0] += body == 1
                v[1] += body is not None
                v[2] += formal == 1
                v[3] += formal is not None
            b["rec"][row["recommendation"]] += 1
            b["sent"][reputation_label(row["reputation_code"])] += 1
            qid = row["question_id"]
            reference = {"q":qid, "text":prompts.get(qid), "d":dim}
            if row["recommendation"] == "neutral":
                neutral.append(reference)
            if not rank and "evidence" not in b:
                b["evidence"] = {"q":qid, "text":prompts.get(qid)}
        expected = db.execute("SELECT count(*) FROM sample WHERE status='valid'").fetchone()[0]
        issues = dict(db.execute("SELECT code,count(*) FROM issue GROUP BY code"))
    api.require(valid > 0 and expected == valid == sum(b["n"] for b in buckets.values()),
                "快照校验 / 有效回答或分组计数未闭合")
    api.require(set(sites) == valid_tasks and
                all(set(site) == {"Kalodata","FastMoss"} and
                    all(body in (0,1) and formal in (None,0,1) for body,formal in site.values())
                    for site in sites.values()), "快照校验 / 官网链接判定缺失或无效")
    return {"batch":batch, "platform":native, "rows":list(buckets.values()), "issues":issues,
            "markets":{v:k for k,v in api.MARKETS.items()}, "competitors":{v:market_brands(v) for v in api.MARKETS.values()},
            "tags":tag_names, "groups":groups, "neutral":neutral, "links":{"answerbit":SOURCE,"bank":QNA}}


def render(data, output):
    page = ASSET.read_text()
    api.require(page.count("__GEO_PAYLOAD__") == 1, "渲染 / 看板模板缺少唯一数据占位符")
    api.write_atomic(output, page.replace("__GEO_PAYLOAD__", encode(data)).encode())


def encode(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<","\\u003c")


def retained_data(output):
    before, rest = output.read_text().split(DATA_MARKER,1)
    payload, after = rest.split("</script>",1)
    return before, json.loads(payload), after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("refresh","snapshot"))
    parser.add_argument("bank", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--begin")
    parser.add_argument("--end")
    parser.add_argument("--fetch-platform", action="store_true")
    args = parser.parse_args()
    stage = "配置"
    try:
        api.require(args.output.suffix.lower() == ".html" and args.snapshot.suffix == ".sqlite"
                    and len({p.resolve() for p in (args.output,args.bank,args.snapshot)}) == 3,
                    "配置 / 题库、SQLite 与 HTML 路径必须独立")
        today = datetime.now(CHINA).date()
        begin = api.date_arg(args.begin or str(today-timedelta(days=6)))
        end = api.date_arg(args.end or str(today))
        api.require(begin <= end, "配置 / 起始日应早于或等于截止日")
        native = None
        if args.mode == "snapshot":
            with closing(sqlite3.connect(args.snapshot.resolve().as_uri()+"?mode=ro", uri=True)) as db:
                begin,end = db.execute("SELECT begin_date,end_date FROM batch").fetchone()
        if args.mode == "refresh" or args.fetch_platform:
            stage = "只读凭证"
            key,brand = api.operator_credentials()
            call = PacedRead(key)
            if args.mode == "refresh":
                stage = "Answerbit 回答取数与对账"
                for attempt in range(2):
                    try:
                        refresh_answers(args.bank,args.snapshot,brand,begin,end,call)
                        break
                    except api.ArchiveError as error:
                        if attempt or not str(error).startswith("分页 /"):
                            raise
            stage = "Answerbit 原生图表快照"
            native = platform_snapshot(call,brand,begin,end)
            if args.output.is_file() and native["issues"]:
                try:
                    _, previous, _ = retained_data(args.output)
                except (OSError, ValueError, IndexError):
                    previous = {}
                available = (previous.get("platform") or {}).get("data", {})
                failed = [path for path, reason in native["issues"].items()
                          if path in available and reason != "响应无可用数据"]
                api.require(not failed, "原生快照刷新失败 / " + "、".join(failed))
        stage = "看板校验与渲染"
        data = dashboard_data(args.snapshot,args.bank,native)
        render(data,args.output)
        status = args.output.with_suffix(".status.json")
        status.unlink(missing_ok=True)
        print(json.dumps({"dashboard":str(args.output.resolve()),"mode":args.mode,
                          "valid":sum(b["n"] for b in data["rows"]), "batch":data["batch"],
                          "native_available":list(native["data"]) if native else [],
                          "native_issues":native["issues"] if native else {}},ensure_ascii=False))
        return 0
    except (api.ArchiveError, OSError, sqlite3.Error, ValueError, KeyError) as error:
        why = str(error) if isinstance(error, api.ArchiveError) else "文件、数据库或响应结构异常"
        status = {"stage":stage,"error":why,"attempt":datetime.now(CHINA).isoformat(timespec="seconds"),
                  "retained":str(args.output.resolve()) if args.output.is_file() else None,
                  "last_success":None}
        if status["retained"]:
            try:
                before, old_data, after = retained_data(args.output)
                status["last_success"] = old_data["batch"]["fetched_at_utc"]
                api.write_atomic(args.output.with_suffix(".status.json"),json.dumps(status,ensure_ascii=False).encode())
                old_data["last_failure"] = status
                api.write_atomic(args.output, (before + DATA_MARKER + encode(old_data) + "</script>" + after).encode())
            except (OSError, ValueError, IndexError, KeyError):
                pass
        print(json.dumps(status,ensure_ascii=False),file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
