"""Read existing Answerbit answers into a numbered local SQLite snapshot."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from getpass import GetPassWarning, getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import warnings
from unicodedata import normalize
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit
from zipfile import BadZipFile

from archive import ArchiveError, URL, citation_flag, domain_hit, host, identifier, read_json, require, write_atomic
from content_metrics import (KALODATA, first_ranks, recommendation, reputation_label,
                             strip_opening_echo, summarize_content, named_prompt)
from workbook_export import write_question_check
from answer_quality import assess as assess_quality

BASE = "https://answerbit.qq.com"
LIST = "/geo/task/get"
DETAIL = "/geo/task/detail/get"
PLATFORM_READS = ("/geo/base/dashboard", "/geo/exposure/rank", "/geo/score/rank", "/geo/exposure/trends",
                  "/geo/score/trends", "/geo/domain/rank", "/geo/article/rank")
PAGE_SIZE = 50
MARKETS = dict(zip(
    ("美国", "英国", "德国", "法国", "中国", "泰国", "越南", "印尼", "马来西亚", "日本", "韩国"),
    ("US", "GB", "DE", "FR", "CN", "TH", "VN", "ID", "MY", "JP", "KR"),
))
VERSION = "answerbit-observed/4-quality"
WEBSITES = (("Kalodata", "kalodata.com"), ("FastMoss", "fastmoss.com"))
BODY_URL = re.compile(r"""(?:[a-z][a-z0-9+.-]*:)?//[^\s<>"'\x60\[\]）】，。；！？、]+""", re.I)
BARE_URL = re.compile(
    r"""(?<![a-z0-9_.@/-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?![a-z0-9-]|\.[a-z0-9-])"""
    r"""(?:[/?#][^\s<>"'\x60\[\]）】，。；！？、]*)?""",
    re.I,
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class QuestionNumberError(ArchiveError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__(f"题号对账 / {len(issues)} 条有题库候选的异常记录缺少已确认题号，请核对飞书题库和 Answerbit 市场标签")


def request(path, payload, key):
    require(path in (LIST, DETAIL, *PLATFORM_READS), "取数 / 接口不在只读允许列表")
    req = Request(BASE + path, method="POST", data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "X-API-Key": key})
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            body = read_json(response.read())
    except HTTPError as error:
        with error:
            raise ArchiveError(f"取数 / {path} HTTP {error.code}，请核对 Answerbit 权限或服务状态") from None
    except (URLError, OSError, ValueError):
        raise ArchiveError(f"取数 / {path} 未获得可解析的响应，请核对网络或接口文档") from None
    code = body.get("code")
    require(path != DETAIL or code != 106001,
            "取数 / Answerbit 回答任务不存在（106001），请在大模型回答记录核对")
    shape = list if path in ("/geo/exposure/rank", "/geo/score/rank") else dict
    require(type(code) is int and code == 0 and isinstance(body.get("data"), shape),
            f"取数 / {path} 返回失败或响应结构变化，请核对 Answerbit")
    return body["data"]


def question_text(value):
    return " ".join(normalize("NFC", value).split()) if isinstance(value, str) else ""


def brand_form(text):
    body = normalize("NFKC", URL.sub(" ", text))
    latin = bool(re.search(r"(?<![a-z0-9_])kalo[\s-]*data(?![a-z0-9_])",body,re.I))
    japanese = "カロデータ" in body
    return {(False, False): "none", (True, False): "latin", (False, True): "ja", (True, True): "both"}[
        latin, japanese
    ]


def website_flags(body, sources):
    # Consume complete URLs before matching bare domains so URL paths never count as hosts.
    hosts = set()
    for url in BODY_URL.findall(body):
        url = url.rstrip(".,;!?)]}")
        hosts.add(host("https:" + url if url.startswith("//") else url))
    for url in BARE_URL.findall(BODY_URL.sub(" ", body)):
        hosts.add(host("https://" + url.rstrip(".,;!?)]}")))
    return [(brand, domain_hit(hosts, domain), citation_flag(sources, domain)) for brand, domain in WEBSITES]


def content_flags(body, prompt, market, reputation):
    clean = BARE_URL.sub(" ", BODY_URL.sub(" ", strip_opening_echo(body, prompt)))
    ranks = first_ranks(clean, market)
    label = recommendation(clean, market, ranks)
    code = reputation if type(reputation) is int else None
    sentiment = reputation_label(code)
    return clean, ranks, label, code, sentiment


def load_bank(path):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ArchiveError("题库读取 / 当前 Agent 缺少读取 Excel 的程序组件，请由 Agent 检查运行环境并按项目依赖处理，无需用户提供组件") from None
    content = path.read_bytes()
    try:
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
        with closing(book):
            rows, locations, issues = {}, {}, []
            by_text = defaultdict(list)
            for title in book.sheetnames:
                if title not in MARKETS:
                    continue
                market = MARKETS[title]
                sheet = book[title]
                # Feishu exports can declare A1:A1 even when the worksheet contains data.
                sheet.reset_dimensions()
                values = sheet.iter_rows(values_only=True)
                header = next(values, ())
                needed = ("编号", "问题内容", "分类名称")
                require(all(header.count(name) == 1 for name in needed),
                        f"题库 / {title} 缺少唯一表头，请核对飞书题库")
                positions = [header.index(name) for name in needed]
                for line, row in enumerate(values, 2):
                    if not any(value is not None for value in row):
                        continue
                    number, prompt, category = [row[i] if i < len(row) else None for i in positions]
                    prompt = question_text(prompt)
                    location = f"{title} 第 {line} 行"
                    if not identifier(number):
                        issues.append(f"题库 / {location} 编号无效")
                        continue
                    if number in locations:
                        issues.append(f"题库 / 编号 {number} 重复：{locations[number]}、{location}")
                        continue
                    locations[number] = location
                    if not prompt or not isinstance(category, str) or not category.strip():
                        field = "题文" if not prompt else "分类"
                        issues.append(f"题库 / {location} {number} 缺少有效{field}")
                        continue
                    form = brand_form(prompt)
                    rows[number] = (market, category.strip(), int(form != "none"), form)
                    by_text[prompt].append(number)
            require(not issues, "\n".join(issues + ["请在飞书题库修正后重新导出；已有本地库保留"]))
    except (BadZipFile, KeyError, ValueError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError("题库 / 无法读取，请从飞书重新导出 XLSX") from None
    require(rows, "题库 / 没有找到市场题单，请使用飞书全国家题库导出")
    return hashlib.sha256(content).hexdigest(), rows, by_text


def task_list(call, brand, begin, end, page_size=None):
    tasks, ids = [], set()
    page, expected = 1, None
    while expected is None or len(tasks) < expected:
        # Omitting mention_brand returns only unmentioned records on the live API.
        data = call(LIST, dict(brand_id=brand, begin_date=begin, end_date=end, include_unanalyzed=True, mention_brand=-1,
                               page=page, page_size=page_size or PAGE_SIZE))
        total, records = data.get("total"), data.get("scores")
        require(type(total) is int and total >= 0 and isinstance(records, list)
                and (expected is None or total == expected) and len(tasks) + len(records) <= total
                and (bool(records) or total == 0),
                f"分页 / 第 {page} 页与接口总数不一致，请回 Answerbit 核对")
        expected = total
        for row in records:
            task = row.get("task_id") if isinstance(row, dict) else None
            require(identifier(task) and task not in ids,
                    f"分页 / 第 {page} 页存在无效或重复任务 ID，请回 Answerbit 核对")
            ids.add(task)
        tasks.extend(records)
        page += 1
    return tasks


def record_markets(row):
    tags = row.get("tags")
    return {MARKETS[tag["tag_name"]] for tag in tags
            if isinstance(tag, dict) and isinstance(tag.get("tag_name"), str)
            and tag["tag_name"] in MARKETS} if isinstance(tags, list) else set()


def select_question(row, questions, by_text):
    candidates = by_text.get(question_text(row.get("query_str")), [])
    markets = record_markets(row)
    if markets:
        candidates = [number for number in candidates if questions[number][0] in markets]
    if len(markets) > 1:
        return None, "market_tag_conflict"
    if len(candidates) != 1:
        return None, "ambiguous_question" if candidates else "unmatched_question"
    return candidates[0], None


def platform_question_key(row):
    prompt_id = row.get("query_id") or row.get("prompt_id")
    if isinstance(prompt_id, str) or type(prompt_id) is int:
        return str(prompt_id), question_text(row.get("query_str"))
    return None


def analysis_state(record):
    analysis = record.get("analysis")
    return analysis.get("state") if isinstance(analysis, dict) else None


def question_metadata(row, detail):
    group_id = row.get("title_id") or detail.get("title_id")
    group_name = row.get("title_name") or detail.get("title_name")
    raw_tags = row.get("tags")
    if not isinstance(raw_tags, list):
        raw_tags = detail.get("tags")
    tags, conflicting = {}, set()
    complete = isinstance(raw_tags, list)
    for item in raw_tags if complete else []:
        if not isinstance(item, dict) or not identifier(item.get("tag_id")) or not isinstance(item.get("tag_name"), str):
            complete = False
            continue
        tag_id, name = item["tag_id"], item["tag_name"]
        kind = item.get("tag_type")
        value = (name, kind if type(kind) is int else None)
        if tag_id in tags and tags[tag_id] != value:
            conflicting.add(tag_id)
            complete = False
        tags[tag_id] = value
    return (
        group_id if identifier(group_id) else None,
        group_name if isinstance(group_name, str) and group_name.strip() else None,
        int(complete),
        [(tag_id, *value) for tag_id, value in tags.items() if tag_id not in conflicting],
    )


def date_arg(value):
    try:
        require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value,
                "日期 / 需要 YYYY-MM-DD")
        return value
    except ValueError:
        raise ArchiveError("日期 / 需要 YYYY-MM-DD") from None


def question_alignment(records, questions, by_text, begin, end):
    matches = [select_question(row, questions, by_text) for row in records]
    verified_numbers = defaultdict(set)
    for row, (number, _) in zip(records, matches):
        key = platform_question_key(row)
        if number and key:
            verified_numbers[key].add(number)
    issues = []
    for row, (number, problem) in zip(records, matches):
        task = row["task_id"]
        platform, language, day = row.get("platform"), row.get("language"), row.get("date")
        require(identifier(platform) and identifier(language) and isinstance(day, str) and begin <= day <= end,
                f"记录 / 任务 {task} 的平台、语言或日期不符合查询窗口，请回 Answerbit 核对")
        date_arg(day)
        if problem:
            prompt = question_text(row.get("query_str"))
            candidates = by_text.get(prompt, [])
            markets = record_markets(row)
            if len(markets) == 1:
                candidates = [qid for qid in candidates if questions[qid][0] in markets] or candidates
            verified = verified_numbers.get(platform_question_key(row), set())
            if problem == "ambiguous_question" and not markets and len(verified) == 1:
                number = next(iter(verified))
            issues.append((task, number, platform, day, problem, "飞书题库与 Answerbit 回答记录",
                           prompt or None, "、".join(candidates) if candidates and not number else None,
                           "同批提问 ID 和题文" if number else None))
    return matches, issues


def observed_scope(records):
    keys = set()
    for row in records:
        tags = row.get("tags")
        key = {"prompt":question_text(row.get("query_str")),"platform":row.get("platform"),
               "language":row.get("language"),"group":row.get("title_id"),
               "tags":sorted(json.dumps(t,sort_keys=True,ensure_ascii=False) for t in tags) if isinstance(tags,list) else None}
        keys.add(json.dumps(key,sort_keys=True,ensure_ascii=False))
    return hashlib.sha256(json.dumps(sorted(keys)).encode()).hexdigest()


def refresh(bank, output, brand, begin, end, call, group_id=None, tag_ids=(), *, page_size=None,
            review_unknown=None, store_answers=False, require_issue_numbers=False):
    begin, end = date_arg(begin), date_arg(end)
    require(begin <= end and identifier(brand), "取数 / 日期范围或 Kalodata 品牌 ID 无效")
    require(output.suffix == ".sqlite" and bank.resolve() != output.resolve(),
            "本地库 / 请选择独立的 .sqlite 输出文件")
    version, questions, by_text = load_bank(bank)
    records = task_list(call, brand, begin, end, page_size)
    require(records, "取数 / 当前日期范围无 Answerbit 记录，请核对首轮采样状态；已有本地库保留")
    matches, issues = question_alignment(records,questions,by_text,begin,end)
    if require_issue_numbers:
        missing = [row for row in issues if not row[1] and row[7]]
        if missing:
            raise QuestionNumberError(missing)
    samples, groups, tags, websites, content, prompts = [], [], [], [], [], []
    originals, quality_inputs, review_candidates = [], [], []
    for row, (number, problem) in zip(records,matches):
        if problem:
            continue
        task = row["task_id"]
        platform, language, day = row["platform"], row["language"], row["date"]
        prompts.append((task,question_text(row["query_str"]),named_prompt(row["query_str"])))
        state = analysis_state(row)
        status, mentioned, official, form = "unconfirmed", None, None, None
        detail = {}
        if type(state) is int and state == 2:
            detail = call(DETAIL, dict(brand_id=brand, task_id=task))
            body, links = detail.get("llm_output"), detail.get("links")
            detail_question = detail.get("query") or detail.get("prompt")
            list_prompt_id = row.get("query_id") or row.get("prompt_id")
            detail_prompt_id = detail.get("query_id") or detail.get("prompt_id")
            detail_task = detail.get("task_id")
            detail_state = analysis_state(detail)
            require((detail_task is None or str(detail_task) == task)
                    and question_text(detail_question) == question_text(row.get("query_str"))
                    and detail.get("platform") == platform and detail.get("language") == language
                    and detail.get("date") == day
                    and (not list_prompt_id or not detail_prompt_id or list_prompt_id == detail_prompt_id)
                    and (detail_state is None or type(detail_state) is int and detail_state == 2),
                    f"详情 / 题号 {number} 任务 {task} 与列表不一致，请回 Answerbit 核对")
            quality_inputs.append(dict(task=task, prompt=detail_question, body=body,
                key=(number,question_text(detail_question),platform,language,row.get('zone'),
                     str(detail.get('terminal',row.get('terminal'))),
                     str(detail.get('answer_mode',row.get('answer_mode'))))))
            if store_answers:
                originals.append((task, detail_question, body if isinstance(body,str) else ''))
            if isinstance(body, str) and body.strip():
                status = "valid"
                urls = [item.get("url") if isinstance(item, dict) else None
                        for item in links] if isinstance(links, list) else None
                reputation = detail.get("brand_reputation")
                clean, ranks, label, code, sentiment = content_flags(
                    body, detail_question, questions[number][0], reputation)
                reputation_issue = (None if sentiment != "unknown" else
                                    "missing" if reputation is None else
                                    "format" if type(reputation) is not int else "enum")
                form = brand_form(clean)
                content.append((task, json.dumps(ranks), label, code, sentiment,
                                detail_question if label == "neutral" else None, reputation_issue))
                if label == "unknown" and review_unknown is not None:
                    review_candidates.append((task,body,detail_question,questions[number][0]))
                flags = website_flags(body, urls)
                websites.extend((task, *result) for result in flags)
                mentioned, official = int(KALODATA in ranks), {name: formal for name, _, formal in flags}["Kalodata"]
                if official is None:
                    issues.append((task, number, platform, day, "citation_unknown", "Answerbit 引用字段", None, None, None))
        if status != "valid":
            issues.append((task, number, platform, day, "answer_unconfirmed", "Answerbit 回答分析状态", None, None, None))
        title_id, title_name, tags_known, task_tags = question_metadata(row, detail)
        groups.append((task, title_id, title_name, tags_known))
        tags.extend((task, *tag) for tag in task_tags)
        if not tags_known:
            issues.append((task, number, platform, day, "tag_metadata_incomplete", "Answerbit 提问标签", None, None, None))
        zone = row.get("zone")
        samples.append((task, number, platform, language, day, zone if identifier(zone) else None,
                        status, mentioned, official, form))
    quality = assess_quality(quality_inputs)
    excluded = {task for task,item in quality.items() if item['status'] != 'accepted'}
    samples = [(*r[:6],'data_anomaly',None,None,None) if r[0] in excluded else r for r in samples]
    content = [r for r in content if r[0] not in excluded]
    websites = [r for r in websites if r[0] not in excluded]
    issues = [r for r in issues if not (r[0] in excluded and r[4] in ('answer_unconfirmed','citation_unknown'))]
    prompt_texts = {r[0]:r[1] for r in prompts}
    for task,number,platform,_,day,*_ in samples:
        if task in excluded:
            item = quality[task]
            issues.append((task,number,platform,day,'quality_'+item['status'],
                           'Answerbit 大模型回答记录 / '+item['reason'],prompt_texts[task],None,'原文质量检查'))
    for candidate in review_candidates:
        if candidate[0] not in excluded:
            review_unknown(*candidate)
    valid_count = sum(r[6] == 'valid' for r in samples)
    require(samples, "题号对账 / 无记录能唯一匹配飞书题库，请核对题文、市场标签和题库版本；已有本地库保留")
    require(valid_count > 0,
            f"取数 / 当前窗口没有可判定的有效回答；数据异常 {len(excluded)} 条，请回 Answerbit 核对分析状态及原文；已有本地库保留")

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "answerbit.sqlite"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript("""
                CREATE TABLE batch (
                    source TEXT, begin_date TEXT, end_date TEXT, fetched_at_utc TEXT,
                    question_version TEXT, metric_version TEXT, returned_records INTEGER
                );
                CREATE TABLE question (
                    question_id TEXT PRIMARY KEY, market TEXT, category TEXT, named_prompt INTEGER,
                    prompt_brand_form TEXT
                );
                CREATE TABLE sample (
                    task_id TEXT PRIMARY KEY, question_id TEXT REFERENCES question(question_id),
                    platform TEXT, language TEXT, task_date TEXT, zone TEXT,
                    status TEXT, mentioned INTEGER, official INTEGER, answer_brand_form TEXT
                );
                CREATE TABLE issue (
                    task_id TEXT, question_id TEXT REFERENCES question(question_id), platform TEXT, task_date TEXT,
                    code TEXT, check_at TEXT, question_text TEXT, candidate_question_ids TEXT, match_basis TEXT
                );
                CREATE TABLE sample_group (
                    task_id TEXT PRIMARY KEY REFERENCES sample(task_id), group_id TEXT, group_name TEXT,
                    tags_known INTEGER NOT NULL
                );
                CREATE TABLE sample_tag (
                    task_id TEXT REFERENCES sample(task_id), tag_id TEXT, tag_name TEXT, tag_type INTEGER,
                    PRIMARY KEY (task_id, tag_id)
                );
                CREATE TABLE sample_website (
                    task_id TEXT REFERENCES sample(task_id), brand TEXT, body_hit INTEGER, formal_hit INTEGER,
                    PRIMARY KEY (task_id, brand)
                );
                CREATE TABLE sample_content (
                    task_id TEXT PRIMARY KEY REFERENCES sample(task_id), ranks TEXT NOT NULL,
                    recommendation TEXT NOT NULL, reputation_code INTEGER, sentiment TEXT NOT NULL,
                    prompt TEXT, reputation_issue TEXT
                );
                CREATE TABLE sample_question (
                    task_id TEXT PRIMARY KEY REFERENCES sample(task_id),
                    prompt TEXT NOT NULL, named_prompt INTEGER NOT NULL CHECK(named_prompt IN (0,1))
                );
                CREATE TABLE retrieval_scope (signature TEXT NOT NULL);
                CREATE TABLE sample_quality (
                    task_id TEXT PRIMARY KEY REFERENCES sample(task_id), status TEXT, reason TEXT,
                    units INTEGER, reference_task TEXT, reference_units INTEGER, ratio REAL, version TEXT
                );
            """)
            db.execute("INSERT INTO batch VALUES (?, ?, ?, ?, ?, ?, ?)",
                       ("Answerbit", begin, end, datetime.now(timezone.utc).isoformat(), version, VERSION, len(records)))
            db.executemany("INSERT INTO question VALUES (?, ?, ?, ?, ?)", [(key, *value) for key, value in questions.items()])
            db.executemany("INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", samples)
            db.executemany("INSERT INTO sample_group VALUES (?, ?, ?, ?)", groups)
            db.executemany("INSERT INTO sample_tag VALUES (?, ?, ?, ?)", tags)
            db.executemany("INSERT INTO sample_website VALUES (?, ?, ?, ?)", websites)
            db.executemany("INSERT INTO sample_content VALUES (?, ?, ?, ?, ?, ?, ?)", content)
            db.executemany("INSERT INTO sample_question VALUES (?, ?, ?)", prompts)
            db.execute("INSERT INTO retrieval_scope VALUES (?)",(observed_scope(records),))
            db.executemany('INSERT INTO sample_quality VALUES (?,?,?,?,?,?,?,?)',
                [(task,*(item[k] for k in ('status','reason','units','reference_task','reference_units','ratio','version')))
                 for task,item in quality.items()])
            db.executemany("INSERT INTO issue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", issues)
            if store_answers:
                db.execute("""CREATE TABLE sample_answer (
                    task_id TEXT PRIMARY KEY REFERENCES sample(task_id), prompt TEXT NOT NULL, body TEXT NOT NULL
                )""")
                db.executemany("INSERT INTO sample_answer VALUES (?, ?, ?)", originals)
        report = read_snapshot(db_path, group_id, tag_ids)
        write_atomic(output, db_path.read_bytes())
    return report


def read_snapshot(path, group_id=None, tag_ids=()):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        batch = dict(db.execute("SELECT * FROM batch").fetchone())
        version = batch["metric_version"]
        require(version in ("answerbit-observed/1", "answerbit-observed/2", "answerbit-observed/3", VERSION),
                "本地库 / 指标版本不受支持，请重新刷新")
        has_forms = version != "answerbit-observed/1"
        form = "q.prompt_brand_form" if has_forms else "NULL"
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        has_reputation = "sample_content" in tables
        negative = """EXISTS (SELECT 1 FROM sample_content c WHERE c.task_id=s.task_id
                       AND typeof(c.reputation_code)='integer' AND c.reputation_code=2)""" if has_reputation else "0"
        latin_count = f"COUNT(*) FILTER (WHERE s.answer_brand_form IN ('latin', 'both') AND NOT ({negative}))" if has_forms else "NULL"
        ja_count = f"COUNT(*) FILTER (WHERE s.answer_brand_form IN ('ja', 'both') AND NOT ({negative}))" if has_forms else "NULL"
        metadata_available = {"sample_group", "sample_tag"} <= tables
        prompt_join = "LEFT JOIN sample_question p USING(task_id)" if "sample_question" in tables else ""
        named = "p.named_prompt" if prompt_join else "q.named_prompt"
        if prompt_join:
            require(db.execute("""SELECT count(*) FROM sample s LEFT JOIN sample_question p USING(task_id)
                               WHERE p.task_id IS NULL OR p.prompt='' OR p.named_prompt NOT IN (0,1)""").fetchone()[0]==0,
                    "返回题文校验 / 部分题文或点名判定缺失，请重新取数")
        require(metadata_available or (group_id is None and not tag_ids),
                "本地库 / 此快照没有 Answerbit 分组与标签，请重新刷新")
        conditions, parameters = [], []
        if group_id is not None:
            conditions.append("EXISTS (SELECT 1 FROM sample_group g WHERE g.task_id = s.task_id AND g.group_id = ?)")
            parameters.append(group_id)
        for tag_id in dict.fromkeys(tag_ids):
            conditions.append("EXISTS (SELECT 1 FROM sample_tag f WHERE f.task_id = s.task_id AND f.tag_id = ?)")
            parameters.append(tag_id)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        def summarize(dimensions=(), join=""):
            columns = ["q.market", "s.language", "s.platform", f"{named} AS named_prompt", f"{form} AS prompt_brand_form",
                       *dimensions]
            positions = ", ".join(str(i) for i in range(1, len(columns) + 1))
            results = [dict(row) for row in db.execute(f"""
                SELECT {', '.join(columns)}, {latin_count} AS mentioned_latin, {ja_count} AS mentioned_ja,
                    COUNT(*) AS matched, COUNT(*) FILTER (WHERE s.status = 'valid') AS valid,
                    COUNT(*) FILTER (WHERE s.mentioned=1 AND NOT ({negative})) AS mentioned,
                    COUNT(*) FILTER (WHERE s.mentioned=1) AS all_mentions, COUNT(s.official) AS citation_known,
                    COUNT(*) FILTER (WHERE s.official = 1) AS official
                FROM sample s JOIN question q USING (question_id) {prompt_join} {join} {where}
                GROUP BY {positions} ORDER BY {positions}
            """, parameters)]
            for row in results:
                for metric, num, den in (
                    ("mention_rate", "mentioned", "valid"),
                    ("official_citation_rate", "official", "citation_known"),
                    ("citation_field_coverage", "citation_known", "valid"),
                ):
                    row[metric] = row[num] / row[den] if row[den] else None
            return results

        def summarize_websites(dimensions=(), join=""):
            columns = ", ".join(("w.brand", *dimensions))
            results = [dict(row) for row in db.execute(f"""
                SELECT {columns}, COUNT(w.body_hit) AS valid, SUM(w.body_hit) AS body_hits,
                    COUNT(w.formal_hit) AS formal_known, COALESCE(SUM(w.formal_hit), 0) AS formal_hits
                FROM sample s JOIN sample_website w USING (task_id) {join} {where}
                GROUP BY {columns} ORDER BY {columns}
            """, parameters)]
            for row in results:
                row["body_link_rate"] = row["body_hits"] / row["valid"]
                row["formal_link_rate"] = row["formal_hits"] / row["formal_known"] if row["formal_known"] else None
                row["formal_field_coverage"] = row["formal_known"] / row["valid"]
            return results

        websites_available = "sample_website" in tables
        content = None
        if "sample_content" in tables:
            content_rows = [dict(row) for row in db.execute(f"""
                SELECT c.*, s.question_id, s.task_date, s.platform, q.market, {named} AS named_prompt,
                       g.group_id, g.group_name
                FROM sample s JOIN sample_content c USING (task_id)
                JOIN question q USING (question_id) {prompt_join} LEFT JOIN sample_group g USING (task_id)
                {where} ORDER BY s.task_date, s.platform, s.task_id
            """, parameters)]
            for row in content_rows:
                row["ranks"] = json.loads(row["ranks"])
            tags_by_task = defaultdict(list)
            for row in db.execute("SELECT task_id, tag_id FROM sample_tag ORDER BY tag_id"):
                tags_by_task[row["task_id"]].append(row["tag_id"])
            content = summarize_content(content_rows, tags_by_task)

        summaries = summarize()
        core_num = sum(r["mentioned"] for r in summaries if r["named_prompt"] == 0)
        core_den = sum(r["valid"] for r in summaries if r["named_prompt"] == 0)
        return {
            "batch": batch, "coverage": None, "coverage_reason": "未取得独立的实际启用与采样计划",
            "mention_rate_policy": "exclude_confirmed_negative" if has_reputation else "all_mentions_legacy",
            "core_mention": {
                "name":"提及率","numerator":core_num if has_reputation else None,"denominator":core_den,
                "value":core_num/core_den if core_den and has_reputation else None,
                "excluded_negative":sum(r["all_mentions"]-r["mentioned"] for r in summaries if r["named_prompt"] == 0) if has_reputation else None,
                "prompt_source":"Answerbit 返回题文" if prompt_join else "旧批题库题文回算",
            },
            "filters": {"group_id": group_id, "tag_ids": list(tag_ids)},
            "content": content, "content_status": "available" if content is not None else "需刷新",
            "groups": summaries, "answerbit_metadata_available": metadata_available,
            "website_links": summarize_websites() if websites_available else None,
            "website_links_by_group": summarize_websites(("g.group_id", "g.group_name"),
                "JOIN sample_group g ON g.task_id = s.task_id") if websites_available and metadata_available else None,
            "website_links_by_tag": summarize_websites(("t.tag_id", "t.tag_name", "t.tag_type"),
                "JOIN sample_tag t ON t.task_id = s.task_id") if websites_available and metadata_available else None,
            "question_categories": summarize(("q.category",)),
            "answerbit_groups": summarize(("g.group_id", "g.group_name"),
                                          "LEFT JOIN sample_group g ON g.task_id = s.task_id") if metadata_available else None,
            "answerbit_tags": summarize(("t.tag_id", "t.tag_name", "t.tag_type"),
                                        "JOIN sample_tag t ON t.task_id = s.task_id") if metadata_available else None,
            "issues": [dict(row) for row in db.execute("SELECT * FROM issue ORDER BY task_id")],
        }


def private_resources(key):
    require(key and key.isascii() and key.isprintable() and not any(char.isspace() for char in key),
            '凭证 / 请先安全提供当前运行者的只读Key，再读取私有资源配置')
    resources = Path(os.environ.get('ANSWERBIT_RESOURCE_CONFIG') or
                     Path(__file__).resolve().parent / '.trial/answerbit-resources.json')
    try:
        config = json.loads(resources.read_text())
        require(isinstance(config,dict), '资源配置 / 本机配置格式无效')
        return config
    except FileNotFoundError:
        return {}
    except (OSError,ValueError):
        raise ArchiveError('资源配置 / 本机资源配置不可读，请由管理员核对；不显示配置内容') from None


def question_bank_url(key):
    require(key and key.isascii() and key.isprintable() and not any(char.isspace() for char in key),
            '凭证 / 请先安全提供只读Key，再读取题库配置')
    url = os.environ.get('GEO_QUESTION_BANK_URL') or private_resources(key).get('question_bank_url')
    if not url:
        return None
    require(isinstance(url,str), '飞书题库 / 私有题库地址须为文本，不回显配置内容')
    try:
        parsed = urlsplit(url)
        require(parsed.scheme=='https' and parsed.hostname and parsed.hostname.endswith('.feishu.cn')
                and parsed.path.startswith(('/wiki/','/sheets/')) and not parsed.username and not parsed.password,
                '飞书题库 / 私有题库地址格式无效；请管理员核对，不回显实际地址')
    except (TypeError,ValueError):
        raise ArchiveError('飞书题库 / 私有题库地址格式无效；请管理员核对') from None
    return url


def operator_credentials():
    key = os.environ.get("ANSWERBIT_API_KEY")
    if not key:
        require(sys.stdin.isatty(), "凭证 / 请由当前运行者在本机环境配置自己的 Answerbit 只读 Key（ANSWERBIT_API_KEY）")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                key = getpass("输入当前运行者的 Answerbit 品牌级只读 Key（隐藏输入）: ")
        except (EOFError, GetPassWarning, KeyboardInterrupt, OSError):
            raise ArchiveError("凭证 / 无法安全获取 Key，请在本机环境配置 ANSWERBIT_API_KEY") from None
    require(key and key.isascii() and key.isprintable() and not any(char.isspace() for char in key),
            "凭证 / Key 为空或格式无效，请重新配置当前运行者的只读 Key")
    brand = os.environ.get("ANSWERBIT_BRAND_ID")
    if not brand:
        brand = private_resources(key).get('brand_id')
    require(identifier(brand), "资源配置 / 已收到只读 Key，但尚未找到 Kalodata 品牌 ID；请由 Agent 先检查已保存的私有配置，确实缺少时再请项目管理员安全补齐，不在聊天回显实际值")
    return key, brand


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pull = commands.add_parser("refresh")
    pull.add_argument("bank", type=Path)
    pull.add_argument("output", type=Path)
    pull.add_argument("--begin", required=True)
    pull.add_argument("--end", required=True)
    query = commands.add_parser("query")
    query.add_argument("output", type=Path)
    for command in (pull, query):
        command.add_argument("--group-id")
        command.add_argument("--tag-id", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "query":
            result = read_snapshot(args.output, args.group_id, args.tag_id)
        else:
            key, brand = operator_credentials()
            bank_url = question_bank_url(key)
            require(bank_url, '飞书题库 / 真实取数前必须配置并核对在线题库，不沿用本地缓存')
            from feishu_bank import fetch_bank
            fetch_bank(bank_url,args.bank)
            result = refresh(args.bank, args.output, brand, args.begin, args.end,
                             lambda path, payload: request(path, payload, key), args.group_id, args.tag_id,
                             require_issue_numbers=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except QuestionNumberError as error:
        path = args.output.with_suffix(".question-check.xlsx")
        try:
            require(path.resolve()!=args.bank.resolve(),"题号核对清单与输入题库路径冲突")
            write_question_check(path,error.issues)
            print(f"{error}；核对清单：{path.resolve()}",file=sys.stderr)
        except (ArchiveError,OSError,ValueError):
            print(f"{error}；无法写入核对清单",file=sys.stderr)
    except ArchiveError as error:
        print(error, file=sys.stderr)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        print("本地文件 / 题库或数据库不可读写，请核对路径与格式；已有本地库保留", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
