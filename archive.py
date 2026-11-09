"""Prepare and distribute read-only GEO pilot snapshots."""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit

INDEX = "latest-demo.json"
VERSION = "geo-demo/1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
DIMENSIONS = ("question_id", "language", "platform", "round")
MARKETS = {"US", "GB", "DE", "FR", "CN", "TH", "VN", "ID", "MY", "JP", "KR"}
BRAND = re.compile(r"(?<![A-Za-z0-9_])kalo\s*data(?![A-Za-z0-9_])", re.I)
URL = re.compile(r"""https?://[^\s<>"'`\]]+""", re.I)
COUNTS = """
    COUNT(*) AS planned,
    COUNT(*) FILTER (WHERE status = 'valid') AS valid,
    COUNT(*) FILTER (WHERE status = 'missing') AS missing,
    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
    COUNT(*) FILTER (WHERE mentioned = 1) AS mentioned,
    COUNT(official) AS citation_known,
    COUNT(*) FILTER (WHERE official = 1) AS official
"""


class ArchiveError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ArchiveError(message)


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def timestamp(value):
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.utcoffset() is not None:
                return moment
        except ValueError:
            pass
    raise ArchiveError("预检 / 时间格式无效或缺时区，请核对批次配置")


def host(url):
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme in ("http", "https") and not parsed.username and not parsed.password:
            name = (parsed.hostname or "").lower().rstrip(".")
            if re.fullmatch(r"[a-z0-9.-]+", name):
                return name
    except ValueError:
        pass
    return ""


def answer_flags(body, sources):
    mentioned = int(bool(BRAND.search(URL.sub(" ", body))))
    hosts = [host(link) for link in sources] if isinstance(sources, list) else None
    if hosts is None or not all(hosts):
        return mentioned, None
    return mentioned, int(any(h == "kalodata.com" or h.endswith(".kalodata.com") for h in hosts))


def write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(content)
            temp.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(content):
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise ArchiveError("文件校验 / JSON 无法解析，请检查输入文件或批次索引") from None
    require(isinstance(value, dict), "文件校验 / JSON 顶层必须为对象")
    return value


def unit(row, label):
    require(isinstance(row, dict) and all(identifier(row.get(k)) for k in DIMENSIONS),
            f"{label} / 缺少有效的题号、语言、平台或轮次")
    return tuple(row[key] for key in DIMENSIONS)


def label_for(key):
    return " / ".join(key)


def assess(data):
    batch = data.get("batch")
    require(isinstance(batch, dict) and batch.get("synthetic") is True,
            "预检 / 首版只接受明确标记为 synthetic 的测试批次")
    for field in ("id", "question_version"):
        require(identifier(batch.get(field)), f"预检 / 批次字段 {field} 无效")
    batch_id = batch["id"]
    require(batch.get("retrieval_complete") is True,
            f"取数 / {batch_id} 数据未取全，请回 Answerbit 核对读取范围和分页")
    start = timestamp(batch.get("start"))
    end = timestamp(batch.get("end"))
    require(start < end, f"预检 / {batch_id} 采样开始时间必须早于结束时间")

    plan = data.get("plan")
    require(isinstance(plan, list) and plan, f"对账 / {batch_id} 计划分母未知，请核对 Answerbit 启用题单")
    planned = {}
    questions = {}
    for index, row in enumerate(plan, 1):
        key = unit(row, f"题单第 {index} 条")
        title = label_for(key)
        market, prompt = row.get("market"), row.get("question")
        require(isinstance(market, str) and market in MARKETS
                and isinstance(prompt, str) and prompt.strip(),
                f"题单 / {title} 缺少有效市场或题文，请核对飞书题库")
        question_key = key[:2]
        require(questions.setdefault(question_key, (market, prompt.strip())) == (market, prompt.strip()),
                f"题单 / {title} 题号对应多个题文或市场，请核对飞书题库")
        require(key not in planned, f"题单 / {title} 计划单元重复，请核对 Answerbit 启用题单")
        planned[key] = market, prompt.strip()

    samples = data.get("samples")
    require(isinstance(samples, list), f"取数 / {batch_id} 缺少样本列表，请核对 Answerbit")
    records, results, issues = {}, {}, []
    for index, row in enumerate(samples, 1):
        key = unit(row, f"样本第 {index} 条")
        title = label_for(key)
        record_id = row.get("record_id")
        require(identifier(record_id), f"样本 / {title} 缺少有效记录 ID，请核对 Answerbit")
        require(key in planned, f"对账 / {title} 记录 {record_id} 不在启用题单，请核对 Answerbit")
        _, question = planned[key]
        sample_question = row.get("question")
        require(isinstance(sample_question, str) and sample_question.strip() == question,
                f"题文冲突 / {title} 记录 {record_id}，请对照飞书题库与 Answerbit")
        collected = timestamp(row.get("collected_at"))
        require(start <= collected < end, f"时间窗 / {title} 记录 {record_id} 越界，请核对 Answerbit")
        status, body, sources = row.get("status"), row.get("answer"), row.get("citations")
        require(status in ("valid", "failed"), f"样本 / {title} 记录 {record_id} 状态未知，请核对 Answerbit")
        require(status == "failed" or isinstance(body, str) and body.strip(),
                f"样本 / {title} 记录 {record_id} 缺少有效回答，请核对 Answerbit")
        evidence = (key, collected, status, body, sources)
        if record_id in records:
            require(records[record_id] == evidence,
                    f"重复冲突 / {title} 记录 {record_id} 内容不一致，请核对 Answerbit")
            issues.append(("duplicate", title, record_id, "Answerbit 回答记录"))
            continue
        records[record_id] = evidence
        require(key not in results,
                f"重复冲突 / {title} 有多个记录 ID，请在 Answerbit 确认有效样本")
        mentioned, official = None, None
        if status == "valid":
            mentioned, official = answer_flags(body, sources)
            if official is None:
                issues.append(("citation_unknown", title, record_id, "Answerbit 引用字段"))
        else:
            issues.append(("failed", title, record_id, "Answerbit 回答记录"))
        results[key] = status, record_id, collected.isoformat(), mentioned, official

    rows = []
    for key, (market, question) in sorted(planned.items()):
        result = results.get(key)
        if result is None:
            issues.append(("missing", label_for(key), None, "Answerbit 监测任务"))
            result = "missing", None, None, None, None
        rows.append((*key, market, int(bool(BRAND.search(URL.sub(" ", question)))), *result))
    meta = {key: batch[key] for key in ("id", "question_version", "start", "end", "synthetic")}
    meta["metric_version"] = VERSION
    return meta, rows, issues


def prepare(source, draft):
    meta, rows, issues = assess(read_json(source.read_bytes()))
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE batch (metadata TEXT NOT NULL);
            CREATE TABLE sample (
                question_id TEXT, language TEXT, platform TEXT, round TEXT, market TEXT,
                named_prompt INTEGER, status TEXT, record_id TEXT UNIQUE, collected_at TEXT,
                mentioned INTEGER, official INTEGER,
                PRIMARY KEY (question_id, language, platform, round)
            );
            CREATE TABLE issue (code TEXT, sample_key TEXT, record_id TEXT, check_at TEXT);
        """)
        with db:
            db.execute("INSERT INTO batch VALUES (?)", (json.dumps(meta),))
            db.executemany("INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            db.executemany("INSERT INTO issue VALUES (?, ?, ?, ?)", issues)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.sqlite"
            with closing(sqlite3.connect(path)) as file_db:
                db.backup(file_db)
            write_atomic(draft, path.read_bytes())
    return inspect(draft)


def inspect(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "快照校验 / SQLite 文件损坏")
        meta = read_json(db.execute("SELECT metadata FROM batch").fetchone()[0])
        require(meta.get("synthetic") is True and meta.get("metric_version") == VERSION
                and identifier(meta.get("id")), "快照校验 / 批次标记或指标版本不匹配")
        totals = dict(db.execute(f"SELECT {COUNTS} FROM sample").fetchone())
        require(totals["planned"] > 0, "快照校验 / 缺少计划单元")
        groups = [
            dict(row) for row in db.execute(
                f"SELECT market, language, platform, round, named_prompt, {COUNTS} FROM sample "
                "GROUP BY market, language, platform, round, named_prompt ORDER BY 1, 2, 3, 4, 5"
            )
        ]
        for result in [totals, *groups]:
            for name, numerator, denominator in (
                ("coverage", "valid", "planned"),
                ("mention_rate", "mentioned", "valid"),
                ("official_citation_rate", "official", "citation_known"),
                ("citation_field_coverage", "citation_known", "valid"),
            ):
                result[name] = result[numerator] / result[denominator] if result[denominator] else None
        return {
            "batch": meta,
            "totals": totals,
            "groups": groups,
            "issues": [dict(row) for row in db.execute("SELECT * FROM issue ORDER BY code, sample_key")],
        }


def publish(draft, shared):
    # Inspect the same bytes that will be published, even if another prepare replaces the draft.
    content = draft.read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "snapshot.sqlite"
        copy.write_bytes(content)
        meta = inspect(copy)["batch"]
    digest = hashlib.sha256(content).hexdigest()
    name = f"demo-{meta['id']}-{digest}.sqlite"
    write_atomic(shared / name, content)
    entry = {"batch_id": meta["id"], "file": name, "sha256": digest, "synthetic": True}
    write_atomic(shared / INDEX, (json.dumps(entry, indent=2) + "\n").encode())
    return entry


def sync(shared, local):
    entry = read_json((shared / INDEX).read_bytes())
    batch_id, digest = entry.get("batch_id"), entry.get("sha256")
    require(entry.get("synthetic") is True and identifier(batch_id)
            and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "同步 / 测试批次索引无效，请找 GEO 负责人核对发布目录")
    name = f"demo-{batch_id}-{digest}.sqlite"
    require(entry.get("file") == name, "同步 / 索引文件名无效，请找 GEO 负责人核对发布目录")
    if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == digest:
        return entry
    content = (shared / name).read_bytes()
    require(hashlib.sha256(content).hexdigest() == digest,
            "同步 / 快照校验值不一致，请找 GEO 负责人重新发布")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "snapshot.sqlite"
        copy.write_bytes(content)
        require(inspect(copy)["batch"]["id"] == batch_id, "同步 / 快照与索引批次不一致")
    write_atomic(local, content)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, paths in (
        ("prepare", ("source", "draft")), ("publish", ("draft", "shared")),
        ("sync", ("shared", "local")), ("query", ("local",)),
    ):
        subparser = commands.add_parser(command)
        for path in paths:
            subparser.add_argument(path, type=Path)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "sync":
            try:
                result = sync(**args)
            except (ArchiveError, OSError, sqlite3.Error, TypeError) as error:
                try:
                    cached = inspect(args["local"])["batch"]
                except (ArchiveError, OSError, sqlite3.Error, TypeError):
                    cached = None
                reason = str(error) if isinstance(error, ArchiveError) else "共享目录或快照不可读"
                print(json.dumps({"status": "sync_failed", "reason": reason, "retained_batch": cached},
                                 ensure_ascii=False), file=sys.stderr)
                return 1
        else:
            action = {"prepare": prepare, "publish": publish, "query": inspect}[command]
            result = action(*args.values())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
    except (OSError, sqlite3.Error, TypeError):
        print(f"{command} / 文件无法读取、写入或校验，请核对指定路径和文件格式", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
