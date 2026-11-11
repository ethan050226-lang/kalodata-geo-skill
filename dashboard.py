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
from tempfile import TemporaryDirectory
from threading import Lock
import time

import answerbit as api
from content_metrics import KALODATA, market_brands, reputation_label
from recommendation_review import identity, review_result, run_review, saved_review, sidecar, write_review
from workbook_export import export_workbook, write_question_check

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
    reviewed = {}
    remaining = iter(())

    def review_unknown(task,body,prompt,market):
        reviewed[task] = review_result(body,prompt,market)

    with TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=5) as executor:
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
        staged = Path(directory) / "refresh.sqlite"
        report = api.refresh(bank,staged,brand,begin,end,fetch,page_size=500,
                             review_unknown=review_unknown,store_answers=True,require_issue_numbers=True)
        api.require(len(reviewed)==report["content"]["recommendation"]["unknown"],
                    "推荐复检 / 当次未知任务总数未闭合")
        write_review(staged,identity(staged),reviewed)
        api.write_atomic(snapshot,staged.read_bytes())
        api.write_atomic(sidecar(snapshot),sidecar(staged).read_bytes())
    return report


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def platform_snapshot(call, brand, begin, end):
    results, issues, competitors = {}, {}, {}
    competitor_ids = []
    payload = dict(brand_id=brand, begin_date=begin, end_date=end)
    def trend_points(rows, field):
        return [{"date": r["date"], "value": number(r.get(field)), "count": number(r.get("task_count")),
                 "rank": number(r.get("avg_rank"))}
                for r in rows if isinstance(r, dict) and isinstance(r.get("date"), str)
                and begin <= r["date"] <= end and number(r.get(field)) is not None]
    for path in api.PLATFORM_READS:
        try:
            paged = path in ("/geo/domain/rank", "/geo/article/rank")
            options = {"page":1, "page_size":5} if paged else {}
            if path.endswith("/trends") and competitor_ids:
                options["competitor_ids"] = competitor_ids
            data = call(path, {**payload, **options})
            if path.endswith("/dashboard"):
                values = {k: number(data[k].get("value")) for k in ("exposure","avg_rank","score")
                          if isinstance(data.get(k), dict)}
                values = {k:v for k,v in values.items() if v is not None}
            elif path.endswith("/trends"):
                field = "exposure" if "exposure" in path else "score"
                values = trend_points(data.get("brand_statistics", []),field)
                competitors[path] = [{"label": r["name"], "points": trend_points(r.get("statistics",[]),field)}
                                     for r in data.get("competitor_statistics",[]) if isinstance(r,dict)
                                     and isinstance(r.get("name"),str)]
            elif paged:
                field = "domain" if "/domain/" in path else "article"
                values = [{"label": r[field], "count": r["count"]} for r in data.get("reference_count", [])[:5]
                          if isinstance(r, dict) and isinstance(r.get(field), str)
                          and type(r.get("count")) is int and r["count"] >= 0]
            else:
                field = "exposure" if "/exposure/" in path else "score"
                if field == "exposure":
                    competitor_ids = list(dict.fromkeys(str(r["competitor_id"]) for r in data
                        if isinstance(r,dict) and str(r.get("competitor_id","")).isdigit()
                        and str(r["competitor_id"]) != "0"))[:10]
                values = [{"label": r["competitor_name"], "value": number(r.get(field)),
                           "rank": number(r["avg_rank"].get("value")) if isinstance(r.get("avg_rank"), dict) else None,
                           "own": str(r.get("competitor_id")) == "0"}
                          for r in data if isinstance(r, dict) and isinstance(r.get("competitor_name"), str)
                          and number(r.get(field)) is not None]
            if values:
                results[path] = values
            else:
                issues[path] = "响应无可用数据"
        except (api.ArchiveError, TypeError, KeyError) as error:
            issues[path] = str(error) if isinstance(error, api.ArchiveError) else "响应结构待核对"
    return {"fetched": datetime.now(CHINA).isoformat(timespec="seconds"), "begin":begin, "end":end,
            "data":results, "issues":issues, "competitor_trends":competitors}


def dashboard_data(snapshot, bank, native):
    sha, _, by_text = api.load_bank(bank)
    prompts = {qid: text for text, ids in by_text.items() for qid in ids}
    review = saved_review(snapshot)
    review_rows = []
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        batch = dict(db.execute("SELECT * FROM batch").fetchone())
        api.require(batch["question_version"] == sha, "题库校验 / 快照和题库版本不一致")
        api.require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "快照校验 / SQLite 文件校验失败")
        has_originals = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sample_answer'").fetchone()
        recommendations = [] if has_originals else None
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
        columns = {r["name"] for r in db.execute("PRAGMA table_info(sample_content)")}
        reason_column = "c.reputation_issue" if "reputation_issue" in columns else "NULL"
        for row in db.execute(f"""
            SELECT s.task_id,s.question_id,s.platform,s.task_date,q.market,q.named_prompt,g.group_id,g.group_name,
                   c.ranks,c.recommendation,c.reputation_code,{reason_column} AS reputation_issue
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
                buckets[dim] = {"d": dim, "n":0, "mentions":Counter(), "negative_mentions":0, "first":0, "rank":[0,0],
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
            rec = row["recommendation"]
            sentiment = reputation_label(row["reputation_code"])
            b["negative_mentions"] += bool(rank) and sentiment == "negative"
            b["rec"][rec] += 1
            b["sent"][sentiment] += 1
            if sentiment == "unknown":
                reason = row["reputation_issue"] or ("enum" if row["reputation_code"] is not None else "unrecorded")
                b.setdefault("sent_issues", Counter())[reason] += 1
            qid = row["question_id"]
            reference = {"q":qid, "text":prompts.get(qid), "d":dim}
            result = None
            if review and rec == "unknown":
                result = review["records"].get(task)
                api.require(result is not None, "推荐复检 / 任务未在复检记录中")
                review_rows.append({**result, **reference, "task":task})
            if recommendations is not None:
                judgment = result["label"] if result and result["status"] == "ok" else rec
                if judgment == "negative":
                    judgment = "no_recommendation"
                if judgment != "explicit":
                    recommendations.append({**(result or {}), **reference, "task":task, "label":judgment})
            if rec == "neutral":
                neutral.append(reference)
            if reference["text"]:
                signals = {
                    "absence": not named and (not rank or sentiment == "negative"),
                    "formal": bool(rank) and sites[task].get(KALODATA,[None,None])[1] == 0,
                    "rank": bool(rank and rank > 1),
                    "sentiment": sentiment in ("negative", "mixed"),
                    "recommendation": rec == "unknown",
                }
                for signal, matches in signals.items():
                    if matches:
                        b.setdefault("evidence", {}).setdefault(signal, {"q":qid, "text":reference["text"]})
        expected = db.execute("SELECT count(*) FROM sample WHERE status='valid'").fetchone()[0]
        issues = dict(db.execute("SELECT code,count(*) FROM issue GROUP BY code"))
        if has_originals:
            originals = db.execute("""
                SELECT COUNT(*) FROM sample_answer JOIN sample USING(task_id)
                WHERE status='valid' AND length(body)>0
            """).fetchone()[0]
            api.require(originals == valid, "本地原文库 / 有效回答和原文条数不一致，请重新运行")
    api.require(valid > 0 and expected == valid == sum(b["n"] for b in buckets.values()),
                "快照校验 / 有效回答或分组计数未闭合")
    api.require(set(sites) == valid_tasks and
                all(set(site) == {"Kalodata","FastMoss"} and
                    all(body in (0,1) and formal in (None,0,1) for body,formal in site.values())
                    for site in sites.values()), "快照校验 / 官网链接判定缺失或无效")
    if review:
        api.require(len(review_rows)==review["total"]==len(review["records"]),
                    "推荐复检 / 任务总数未闭合")
    return {"batch":batch, "platform":native, "rows":list(buckets.values()), "issues":issues,
            "markets":{v:k for k,v in api.MARKETS.items()}, "competitors":{v:market_brands(v) for v in api.MARKETS.values()},
            "tags":tag_names, "groups":groups, "neutral":neutral, "links":{"answerbit":SOURCE,"bank":QNA},
            "review":{"rows":review_rows,"version":review["version"],"checked_at":review["checked_at"]} if review else None,
            "recommendations":recommendations}


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


def report_metrics(data):
    rows = data["rows"]
    valid = sum(r["n"] for r in rows)
    native = ((data.get("platform") or {}).get("data") or {}).get("/geo/base/dashboard",{})
    metrics = [{"id":"valid","name":"有效回答","source":"Skill","kind":"count",
                "value":valid,"numerator":valid,"denominator":None}]
    for key, title, kind in (("exposure","品牌提及率","percent"),("avg_rank","平均排名","rank"),
                             ("score","曝光效果分数","score")):
        if number(native.get(key)) is not None:
            metrics.append({"id":"platform."+key,"name":title,"source":"Answerbit 平台快照","kind":kind,
                            "value":native[key],"numerator":None,"denominator":None})
    if len(metrics)>1:
        return metrics
    unnamed = [r for r in rows if not r["d"][4]]
    known_sentiment = all("negative_mentions" in r for r in rows)
    mention = sum((r["mentions"].get("Kalodata",0)-r.get("negative_mentions",0)) for r in rows)
    unnamed_mention = sum((r["mentions"].get("Kalodata",0)-r.get("negative_mentions",0)) for r in unnamed)
    unnamed_count = sum(r["n"] for r in unnamed)
    official = sum(r["links"].get("Kalodata",[0,0,0,0])[2] for r in rows)
    official_known = sum(r["links"].get("Kalodata",[0,0,0,0])[3] for r in rows)
    for key, title, num, den, known in (
        ("mention_rate","Kalodata 提及率",mention,valid,known_sentiment),
        ("unnamed_mention_rate","未点名题提及率",unnamed_mention,unnamed_count,known_sentiment),
        ("official_rate","官网正式来源命中率",official,official_known,True),
    ):
        metrics.append({"id":"skill."+key,"name":title,"source":"Skill","kind":"percent",
                        "value":100*num/den if den and known else None,
                        "numerator":num if known else None,"denominator":den})
    return metrics


def previous_comparison(data, previous, *, refresh=False):
    current = report_metrics(data)
    batch = data["batch"]
    baseline = None
    earlier = []
    if previous:
        if previous["batch"] != batch:
            baseline, earlier = previous["batch"], report_metrics(previous)
        elif not refresh and previous.get("comparison"):
            baseline = previous["comparison"]["previous_batch"]
            earlier = previous["comparison"]["previous_metrics"]
    reasons = []
    if baseline:
        for key, label in (("question_version","题库版本"),("metric_version","指标版本")):
            if batch[key] != baseline[key]:
                reasons.append(label+"不同")
        length = lambda b: datetime.fromisoformat(b["end_date"])-datetime.fromisoformat(b["begin_date"])
        if length(batch) != length(baseline):
            reasons.append("数据窗口长度不同")
    before = {r["id"]:r for r in earlier}
    changes = []
    for row in current:
        old = before.get(row["id"])
        value = old["value"] if old else None
        delta = row["value"]-value if baseline and not reasons and row["value"] is not None and value is not None else None
        changes.append({**row,"previous":value,"change":delta})
    return {"previous_batch":baseline,"previous_metrics":earlier,"current_batch":batch,
            "metrics":changes,"limitations":reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("refresh","snapshot","review"))
    parser.add_argument("bank", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--begin")
    parser.add_argument("--end")
    parser.add_argument("--fetch-platform", action="store_true")
    parser.add_argument("--defer-unavailable", action="store_true", help="复检时暂缓上轮未读到的任务")
    parser.add_argument("--excel", type=Path, help="导出同批可筛选的 .xlsx 记录")
    args = parser.parse_args()
    stage = "配置"
    staging = None
    try:
        api.require(args.output.suffix.lower() == ".html" and args.snapshot.suffix == ".sqlite"
                    and len({p.resolve() for p in (args.output,args.bank,args.snapshot)}) == 3,
                    "配置 / 题库、SQLite 与 HTML 路径必须独立")
        api.require(not args.excel or (args.excel.suffix.lower()==".xlsx" and args.excel.resolve()
                    not in {p.resolve() for p in (args.bank,args.snapshot,args.output)}),
                    "配置 / Excel 输出必须是独立的 .xlsx 文件")
        today = datetime.now(CHINA).date()
        begin = api.date_arg(args.begin or str(today-timedelta(days=6)))
        end = api.date_arg(args.end or str(today))
        api.require(begin <= end, "配置 / 起始日应早于或等于截止日")
        native = None
        snapshot = args.snapshot
        if args.mode != "snapshot" or args.fetch_platform:
            stage = "只读凭证"
            print("真实取数需要 Kalodata 品牌级只读 Key。常见一周窗口约 3–8 分钟，平台限流可能延长。"
                  "凭证配置完成后才继续。", file=sys.stderr, flush=True)
            key,brand = api.operator_credentials()
            call = PacedRead(key)
        previous = None
        if args.output.is_file():
            try:
                _, previous, _ = retained_data(args.output)
            except (OSError,ValueError,IndexError):
                pass
        if args.mode != "refresh":
            with closing(sqlite3.connect(args.snapshot.resolve().as_uri()+"?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                old_batch = dict(db.execute("SELECT * FROM batch").fetchone())
                begin,end = old_batch["begin_date"],old_batch["end_date"]
            if previous and previous.get("batch") == old_batch:
                native = previous.get("platform")
        if args.mode != "snapshot":
            if args.mode == "review":
                stage = "Answerbit 推荐原文复检"
                run_review(args.bank,args.snapshot,brand,call,
                           lambda done,total: print(f"推荐复检已处理 {done}/{total}",file=sys.stderr,flush=True),
                           defer_unavailable=args.defer_unavailable)
            if args.mode == "refresh":
                stage = "Answerbit 回答取数与对账"
                staging = TemporaryDirectory()
                snapshot = Path(staging.name) / "snapshot.sqlite"
                for attempt in range(2):
                    try:
                        refresh_answers(args.bank,snapshot,brand,begin,end,call)
                        break
                    except api.ArchiveError as error:
                        if attempt or not str(error).startswith("分页 /"):
                            raise
        if args.mode == "refresh" or args.fetch_platform:
            stage = "Answerbit 原生图表快照"
            native = platform_snapshot(call,brand,begin,end)
            if previous and native["issues"]:
                available = (previous.get("platform") or {}).get("data", {})
                failed = [path for path, reason in native["issues"].items()
                          if path in available and reason != "响应无可用数据"]
                api.require(not failed, "原生快照刷新失败 / " + "、".join(failed))
        stage = "看板校验与渲染"
        data = dashboard_data(snapshot,args.bank,native)
        api.require(args.mode!="refresh" or data["review"] is not None,
                    "推荐复检 / 当次复检文件与快照不匹配，请重新运行")
        data["comparison"] = previous_comparison(data,previous,refresh=args.mode=="refresh")
        excel = None
        if args.excel:
            stage = "Excel 记录导出"
            with closing(sqlite3.connect(snapshot.resolve().as_uri()+"?mode=ro",uri=True)) as db:
                columns = {r[1] for r in db.execute("PRAGMA table_info(issue)")}
                if "candidate_question_ids" in columns:
                    missing = db.execute("""
                        SELECT count(*) FROM issue
                        WHERE coalesce(question_id,'')='' AND coalesce(candidate_question_ids,'')<>''
                    """).fetchone()[0]
                    api.require(not missing, f"题号对账 / 有 {missing} 条题库候选尚未确认，请用已核对的题库重新取数")
            excel = export_workbook(snapshot,data)
            stage = "看板校验与渲染"
        if staging:
            api.require(data["recommendations"] is not None,"本地原文库 / 当次原文未入库，请重新运行")
            html = Path(staging.name) / "dashboard.html"
            render(data,html)
            api.write_atomic(args.snapshot,snapshot.read_bytes())
            api.write_atomic(sidecar(args.snapshot),sidecar(snapshot).read_bytes())
            api.write_atomic(args.output,html.read_bytes())
        else:
            render(data,args.output)
        if excel is not None:
            stage = "Excel 文件写入"
            api.write_atomic(args.excel,excel)
        status = args.output.with_suffix(".status.json")
        status.unlink(missing_ok=True)
        print(json.dumps({"dashboard":str(args.output.resolve()),"mode":args.mode,
                          **({"excel":str(args.excel.resolve())} if args.excel else {}),
                          "valid":sum(b["n"] for b in data["rows"]), "batch":data["batch"],
                          "comparison":data["comparison"],
                          "native_available":list(native["data"]) if native else [],
                          "native_issues":native["issues"] if native else {},
                          "review_status":dict(Counter(r["status"] for r in data["review"]["rows"])) if data["review"] else {}},
                         ensure_ascii=False))
        return 0
    except (api.ArchiveError, OSError, sqlite3.Error, ValueError, KeyError) as error:
        why = str(error) if isinstance(error, api.ArchiveError) else "文件、数据库或响应结构异常"
        status = {"stage":stage,"error":why,"attempt":datetime.now(CHINA).isoformat(timespec="seconds"),
                  "retained":str(args.output.resolve()) if args.output.is_file() else None,
                  "last_success":None}
        if isinstance(error,api.QuestionNumberError):
            status["stage"] = "题号对账"
            path = args.output.with_suffix(".question-check.xlsx")
            try:
                api.require(path.resolve() not in {p.resolve() for p in (args.bank,args.excel) if p},
                            "核对清单与题库或 Excel 输出路径冲突")
                write_question_check(path,error.issues)
                status["question_check"] = str(path.resolve())
            except (api.ArchiveError,OSError,ValueError):
                status["question_check_error"] = "无法写入核对清单，请核对路径和权限"
        if status["retained"] and stage != "只读凭证":
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
    finally:
        if staging:
            staging.cleanup()


if __name__ == "__main__":
    sys.exit(main())
