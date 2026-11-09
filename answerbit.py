"""Read existing Answerbit answers into a numbered local SQLite snapshot."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unicodedata import normalize
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile

from archive import ArchiveError, BRAND, URL, answer_flags, identifier, read_json, require, write_atomic

BASE = "https://answerbit.qq.com"
LIST = "/geo/task/get"
DETAIL = "/geo/task/detail/get"
PAGE_SIZE = 50
MARKETS = dict(zip(
    ("美国", "英国", "德国", "法国", "中国", "泰国", "越南", "印尼", "马来西亚", "日本", "韩国"),
    ("US", "GB", "DE", "FR", "CN", "TH", "VN", "ID", "MY", "JP", "KR"),
))
VERSION = "answerbit-observed/1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(path, payload, key):
    require(path in (LIST, DETAIL), "取数 / 接口不在只读允许列表")
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
    require(type(code) is int and code == 0 and isinstance(body.get("data"), dict),
            f"取数 / {path} 返回失败或响应结构变化，请核对 Answerbit")
    return body["data"]


def question_text(value):
    return " ".join(normalize("NFC", value).split()) if isinstance(value, str) else ""


def load_bank(path):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ArchiveError("题库 / 请先安装 requirements.txt 中的 Excel 读取依赖") from None
    content = path.read_bytes()
    try:
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
        with closing(book):
            rows = {}
            by_text = defaultdict(list)
            for title in book.sheetnames:
                if title not in MARKETS:
                    continue
                market = MARKETS[title]
                values = book[title].iter_rows(values_only=True)
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
                    require(identifier(number) and prompt and isinstance(category, str) and category.strip(),
                            f"题库 / {title} 第 {line} 行的编号、题文或分类无效")
                    require(number not in rows, f"题库 / 编号 {number} 重复，请核对飞书题库")
                    rows[number] = (market, category.strip(), int(bool(BRAND.search(URL.sub(" ", prompt)))))
                    by_text[prompt].append(number)
    except (BadZipFile, KeyError, ValueError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError("题库 / 无法读取，请从飞书重新导出 XLSX") from None
    require(rows, "题库 / 没有找到市场题单，请使用飞书全国家题库导出")
    return hashlib.sha256(content).hexdigest(), rows, by_text


def task_list(call, brand, begin, end):
    tasks, ids = [], set()
    page, expected = 1, None
    while expected is None or len(tasks) < expected:
        data = call(LIST, dict(brand_id=brand, begin_date=begin, end_date=end, include_unanalyzed=True,
                               page=page, page_size=PAGE_SIZE))
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


def select_question(row, questions, by_text):
    candidates = by_text.get(question_text(row.get("query_str")), [])
    tags = row.get("tags")
    markets = {MARKETS[tag["tag_name"]] for tag in tags
               if isinstance(tag, dict) and isinstance(tag.get("tag_name"), str)
               and tag["tag_name"] in MARKETS} if isinstance(tags, list) else set()
    if markets:
        candidates = [number for number in candidates if questions[number][0] in markets]
    if len(markets) > 1:
        return None, "market_tag_conflict"
    if len(candidates) != 1:
        return None, "ambiguous_question" if candidates else "unmatched_question"
    return candidates[0], None


def analysis_state(record):
    analysis = record.get("analysis")
    return analysis.get("state") if isinstance(analysis, dict) else None


def date_arg(value):
    try:
        require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value,
                "日期 / 需要 YYYY-MM-DD")
        return value
    except ValueError:
        raise ArchiveError("日期 / 需要 YYYY-MM-DD") from None


def refresh(bank, output, brand, begin, end, call):
    begin, end = date_arg(begin), date_arg(end)
    require(begin <= end and identifier(brand), "取数 / 日期范围或 Kalodata 品牌 ID 无效")
    require(output.suffix == ".sqlite" and bank.resolve() != output.resolve(),
            "本地库 / 请选择独立的 .sqlite 输出文件")
    version, questions, by_text = load_bank(bank)
    records = task_list(call, brand, begin, end)
    require(records, "取数 / 当前日期范围无 Answerbit 记录，请核对首轮采样状态；已有本地库保留")
    samples, issues = [], []
    for row in records:
        task = row["task_id"]
        platform, language, day = row.get("platform"), row.get("language"), row.get("date")
        require(identifier(platform) and identifier(language) and isinstance(day, str) and begin <= day <= end,
                f"记录 / 任务 {task} 的平台、语言或日期不符合查询窗口，请回 Answerbit 核对")
        date_arg(day)
        number, problem = select_question(row, questions, by_text)
        if problem:
            issues.append((task, number, platform, day, problem, "飞书题库与 Answerbit 回答记录"))
            continue
        state = analysis_state(row)
        status, mentioned, official = "unconfirmed", None, None
        if type(state) is int and state == 2:
            detail = call(DETAIL, dict(brand_id=brand, task_id=task))
            body, links = detail.get("llm_output"), detail.get("links")
            detail_question = detail.get("query") or detail.get("prompt")
            list_prompt_id = row.get("query_id") or row.get("prompt_id")
            detail_prompt_id = detail.get("query_id") or detail.get("prompt_id")
            detail_state = analysis_state(detail)
            require(question_text(detail_question) == question_text(row.get("query_str"))
                    and detail.get("platform") == platform and detail.get("language") == language
                    and detail.get("date") == day
                    and (not list_prompt_id or not detail_prompt_id or list_prompt_id == detail_prompt_id)
                    and (detail_state is None or type(detail_state) is int and detail_state == 2),
                    f"详情 / 题号 {number} 任务 {task} 与列表不一致，请回 Answerbit 核对")
            if isinstance(body, str) and body.strip():
                status = "valid"
                urls = [item.get("url") if isinstance(item, dict) else None
                        for item in links] if isinstance(links, list) else None
                mentioned, official = answer_flags(body, urls)
                if official is None:
                    issues.append((task, number, platform, day, "citation_unknown", "Answerbit 引用字段"))
        if status != "valid":
            issues.append((task, number, platform, day, "answer_unconfirmed", "Answerbit 回答分析状态"))
        zone = row.get("zone")
        samples.append((task, number, platform, language, day, zone if identifier(zone) else None,
                        status, mentioned, official))
    require(samples, "题号对账 / 无记录能唯一匹配飞书题库，请核对题文、市场标签和题库版本；已有本地库保留")
    require(any(row[-3] == "valid" for row in samples),
            "取数 / 当前窗口没有可判定的有效回答，请回 Answerbit 核对分析状态；已有本地库保留")

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "answerbit.sqlite"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript("""
                CREATE TABLE batch (
                    source TEXT, begin_date TEXT, end_date TEXT, fetched_at_utc TEXT,
                    question_version TEXT, metric_version TEXT, returned_records INTEGER
                );
                CREATE TABLE question (
                    question_id TEXT PRIMARY KEY, market TEXT, category TEXT, named_prompt INTEGER
                );
                CREATE TABLE sample (
                    task_id TEXT PRIMARY KEY, question_id TEXT REFERENCES question(question_id),
                    platform TEXT, language TEXT, task_date TEXT, zone TEXT,
                    status TEXT, mentioned INTEGER, official INTEGER
                );
                CREATE TABLE issue (
                    task_id TEXT, question_id TEXT, platform TEXT, task_date TEXT, code TEXT, check_at TEXT
                );
            """)
            db.execute("INSERT INTO batch VALUES (?, ?, ?, ?, ?, ?, ?)",
                       ("Answerbit", begin, end, datetime.now(timezone.utc).isoformat(), version, VERSION, len(records)))
            db.executemany("INSERT INTO question VALUES (?, ?, ?, ?)", [(key, *value) for key, value in questions.items()])
            db.executemany("INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", samples)
            db.executemany("INSERT INTO issue VALUES (?, ?, ?, ?, ?, ?)", issues)
        report = read_snapshot(db_path)
        write_atomic(output, db_path.read_bytes())
    return report


def read_snapshot(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        batch = dict(db.execute("SELECT * FROM batch").fetchone())
        groups = [dict(row) for row in db.execute("""
            SELECT q.market, s.language, s.platform, q.named_prompt, COUNT(*) AS matched,
                COUNT(*) FILTER (WHERE s.status = 'valid') AS valid,
                COUNT(*) FILTER (WHERE s.mentioned = 1) AS mentioned,
                COUNT(s.official) AS citation_known,
                COUNT(*) FILTER (WHERE s.official = 1) AS official
            FROM sample s JOIN question q USING (question_id)
            GROUP BY q.market, s.language, s.platform, q.named_prompt ORDER BY 1, 2, 3, 4
        """)]
        for row in groups:
            for metric, num, den in (
                ("mention_rate", "mentioned", "valid"),
                ("official_citation_rate", "official", "citation_known"),
                ("citation_field_coverage", "citation_known", "valid"),
            ):
                row[metric] = row[num] / row[den] if row[den] else None
        return {"batch": batch, "coverage": None, "coverage_reason": "未取得独立的实际启用与采样计划",
                "groups": groups, "issues": [dict(row) for row in db.execute("SELECT * FROM issue ORDER BY task_id")]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pull = commands.add_parser("refresh")
    pull.add_argument("bank", type=Path)
    pull.add_argument("output", type=Path)
    pull.add_argument("--begin", required=True)
    pull.add_argument("--end", required=True)
    commands.add_parser("query").add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "query":
            result = read_snapshot(args.output)
        else:
            key, brand = os.environ.get("ANSWERBIT_API_KEY"), os.environ.get("ANSWERBIT_BRAND_ID")
            require(key and "\n" not in key and "\r" not in key and identifier(brand),
                    "凭证 / 请在获批运行环境配置只读品牌 Key 与 Kalodata 品牌 ID")
            result = refresh(args.bank, args.output, brand, args.begin, args.end,
                             lambda path, payload: request(path, payload, key))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ArchiveError as error:
        print(error, file=sys.stderr)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        print("本地文件 / 题库或数据库不可读写，请核对路径与格式；已有本地库保留", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
