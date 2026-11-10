"""Conservative second-pass recommendation review for saved Answerbit tasks."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import json
import re
import sqlite3

import answerbit as api
from content_metrics import KALODATA, CUE, CONDITION, HEDGE, NEGATION, REPORTED, SPLIT, brand_patterns, strip_opening_echo

VERSION = "recommendation-review/2"
RESULTS = ("explicit","conditional","neutral","negative","unknown")
REASONS = ("description","target","scope","conflict","uncovered")
STATUSES = ("ok","task_missing","read_failed","mismatch","deferred")
FENCE = re.compile(r"(?ms)^ {0,3}(?:`{3,}|~{3,})[^\n]*\n.*?^ {0,3}(?:`{3,}|~{3,})[^\n]*$")
HTML_TABLE = re.compile(r"(?is)<table\b[^>]*>.*?</table\s*>")
SEPARATOR = re.compile(r"^:?-{3,}:?$")
TOOL = re.compile(r"^(?:tools?|platforms?|brands?|products?|software|工具(?:名称)?|平台|品牌|产品|ツール|製品|outil|plateforme|werkzeug|도구)$", re.I)
VERDICT = re.compile(r"^(?:recommend(?:ation|ed)?|verdict|recommend\?|推荐(?:结论|建议)?|是否推荐|推薦|推奨|おすすめ|empfehlung|recommandation|추천)$", re.I)
YES = re.compile(r"^(?:yes|recommended|strongly recommended|highly recommended|oui|ja|推荐|強く推奨|推奨|建议使用|首选|추천)$", re.I)
NO = re.compile(r"^(?:no|not recommended|non|nein|不推荐|不建議|非推奨|추천하지 않음)$", re.I)
CONDITIONAL_YES = re.compile(r"^(?:recommended\s+(?:only\s+)?(?:if|when|for)\s+\S.+|"
                             r"(?:仅|只)(?:在|对|適合|适合).+?(?:推荐|推薦)|(?:如果|若).+?(?:推荐|建议)|"
                             r"nur wenn\s+\S.+|seulement si\s+\S.+|.+(?:場合|なら).*(?:推奨|おすすめ))$",re.I)
SCOPE = re.compile(r"\b(?:for|für|pour)\s+[^.,;!?:]+|(?:适合|面向|对于|面向|対象|向け)[^，。；]+", re.I)
EXT_COND = re.compile(r"\b(?:wenn|falls|sofern|si|seulement si|à condition que)\b|경우|조건|한해서", re.I)
EXT_CUE = re.compile(r"\b(?:empfehle|empfehlen|empfohlen|empfehlenswert|recommande|recommandons|recommandé|privilégiez|"
                     r"top recommendation|i'd choose|i would choose|my pick|go with)\b|优先考虑|第一选择|추천", re.I)
ATTRIBUTED = re.compile(r"^\s*>|\b(?:laut|selon|d'après)\b|によれば|따르면",re.I)


class HtmlTable(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.rows = []
        self.row = self.cell = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("th","td") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("th","td") and self.cell is not None and self.row is not None:
            self.row.append(" ".join(self.cell))
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None


def table_parts(text):
    """Separate real Markdown and HTML tables from prose."""
    text = FENCE.sub("",text)
    tables = [HtmlTable(x.group()).rows for x in HTML_TABLE.finditer(text)]
    lines = HTML_TABLE.sub("\n",text).splitlines()
    remaining = []
    i = 0
    while i < len(lines):
        if i+1 < len(lines) and "|" in lines[i] and "|" in lines[i+1]:
            header, sep = (re.split(r"(?<!\\)\|",line.strip().strip("|")) for line in lines[i:i+2])
            if len(header) == len(sep) >= 2 and all(SEPARATOR.fullmatch(v.strip()) for v in sep):
                rows = [header]
                i += 2
                while i < len(lines) and "|" in lines[i]:
                    row = re.split(r"(?<!\\)\|",lines[i].strip().strip("|"))
                    if len(row) != len(header):
                        break
                    rows.append(row)
                    i += 1
                tables.append(rows)
                continue
        remaining.append(lines[i])
        i += 1
    return "\n".join(remaining), tables


def clean(text):
    return api.BARE_URL.sub(" ",api.BODY_URL.sub(" ",text)).strip(" *_`")


@lru_cache(maxsize=1)
def all_brand_patterns():
    return {name: regex for market in ("CN","JP","US") for name,regex in brand_patterns(market).items()}


def compare_table(rows, patterns):
    if len(rows)<2 or len(rows[0])<2:
        return False
    all_text = " ".join(clean(c) for row in rows for c in row)
    brands = {name for name,regex in patterns.items() if regex.search(all_text)}
    if KALODATA not in brands:
        return False
    if len(brands)>1:
        return True
    columns = [i for i,c in enumerate(rows[0]) if TOOL.fullmatch(clean(c))]
    for i in columns:
        tools = {clean(r[i]).casefold() for r in rows[1:] if len(r)>i}
        if any(patterns[KALODATA].fullmatch(c) for c in tools) and any(
            re.fullmatch(r"[a-z0-9][a-z0-9 ._+-]{1,30}",c) and not patterns[KALODATA].search(c)
            and c not in {"none","other","unknown","n/a","yes","no"} for c in tools
        ):
            return True
    return False


def table_verdicts(rows, brand):
    header, *data = rows
    headers = [clean(c) for c in header]
    target_cols = [i for i,c in enumerate(headers) if brand.fullmatch(c)]
    verdict_cols = [i for i,c in enumerate(headers) if VERDICT.fullmatch(c)]
    tool_cols = [i for i,c in enumerate(headers) if TOOL.fullmatch(c)]
    for row in data:
        if row and VERDICT.fullmatch(clean(row[0])):
            yield from (clean(row[i]) for i in target_cols if len(row)>i)
        if any(len(row)>i and brand.fullmatch(clean(row[i])) for i in tool_cols):
            yield from (clean(row[i]) for i in verdict_cols if len(row)>i)


@lru_cache(maxsize=1)
def directional_cues():
    brand = "(?:"+all_brand_patterns()[KALODATA].pattern+")"
    target = r"\s*(?:\*\*|[「『“”])?"+brand
    positive = re.compile(
        r"(?:\b(?:i|we)\s+(?:(?:would|strongly|highly)\s+)?(?:recommend|suggest)\s+"
        r"(?:(?:using|choosing|trying)\s+)?|(?:^\s*|[,，]\s*|\byou should\s+)(?:use|choose|try)\s+|"
        r"(?:推荐|推薦|建议|建議)(?:你|您)?(?:使用|选择|選擇|试用)?|"
        r"\b(?:ich empfehle|wir empfehlen|je recommande|nous recommandons|privilégiez|i'd choose|i would choose|"
        r"my (?:top recommendation|first choice|pick) is|go with)\s+)"+target+
        "|"+brand+r"(?:\*\*|[」』”])?\s*(?:is\s+(?:(?:the|my|our)\s+)?(?:top|first|best)\s+(?:choice|pick|option)|"
        r"を(?:おすすめ|お勧め|推奨)します|が(?:おすすめ|お勧め)です|를?\s*추천합니다|是(?:我的|我们的)?首[选選]|"
        r"est (?:fortement )?recommandé|(?:ist|wird) (?:ausdrücklich )?empfohlen)"+
        r"|\bich würde"+target+r"\s+empfehlen\b", re.I)
    negative = re.compile(
        r"(?:\b(?:i|we)\s+(?:(?:would|do)\s+not|don't|wouldn't)\s+(?:recommend|choose|use)\s+"
        r"(?:(?:using|choosing)\s+)?|(?:不推荐|不推薦|不建议|不建議)(?:你|您)?(?:使用|选择|選擇)?|"
        r"\b(?:avoid|don't use|n'utilisez pas)\s+)"+target+
        "|"+brand+r"\s*(?:は(?:おすすめ|お勧め)し(?:ない|ません)|을?\s*추천하지 않습니다)",re.I)
    return positive, negative


def classify(body,prompt,market):
    """Return label, one primary reason, and independent comparison-table flag."""
    text = strip_opening_echo(body,prompt)
    prose, tables = table_parts(text)
    patterns = all_brand_patterns()
    brand = patterns[KALODATA]
    comparable = any(compare_table(t,patterns) for t in tables)
    positive, negative = directional_cues()
    decisions = set()
    flags = set()
    explicit_scopes, conditional_scopes = set(), set()
    for row in tables:
        if not row:
            continue
        for cell in table_verdicts(row,brand):
            if YES.fullmatch(cell):
                decisions.add("explicit")
            elif NO.fullmatch(cell):
                decisions.add("negative")
            elif CONDITIONAL_YES.fullmatch(cell):
                decisions.add("conditional")
                conditional_scopes.add(cell.casefold())
            elif cell and (CUE.search(cell) or EXT_CUE.search(cell) or CONDITION.search(cell)):
                flags.add("scope")
    for raw in SPLIT.split(prose):
        line = clean(raw)
        if not line:
            continue
        has_brand = brand.search(line)
        cue = CUE.search(line) or EXT_CUE.search(line)
        cond = CONDITION.search(line) or EXT_COND.search(line)
        direct = positive.search(line)
        neg = negative.search(line)
        if not has_brand:
            # A recommendation aimed at a different known brand is not evidence about Kalodata.
            if cue and not any(rx.search(line) for rx in patterns.values()) and (
                re.search(r"\b(?:it|this tool|that tool)\b|这款|该工具|このツール|이 도구",line,re.I)
            ):
                flags.add("target")
            continue
        if not (cue or direct or neg):
            continue
        if REPORTED.search(line) or ATTRIBUTED.search(raw) or HEDGE.search(line):
            flags.add("conflict")
            continue
        if any(rx.search(line) for name,rx in patterns.items() if name!=KALODATA):
            flags.add("target")
            continue
        if cond and (neg or NEGATION.search(line)):
            flags.add("scope")
        elif neg:
            decisions.add("negative")
        elif direct and not NEGATION.search(line):
            decisions.add("conditional" if cond else "explicit")
            if cond:
                conditional_scopes.add(line.casefold())
            elif (scope:=SCOPE.search(line)):
                explicit_scopes.add(scope.group().casefold())
        elif cond or NEGATION.search(line):
            flags.add("scope")
        else:
            flags.add("uncovered")
    if "negative" in decisions and len(decisions)>1:
        return "unknown","conflict",comparable
    if flags & {"conflict","scope","target"}:
        return "unknown",next(k for k in ("conflict","scope","target") if k in flags),comparable
    if decisions=={"explicit","conditional"}:
        # shortcut: require independently identifiable contexts; vague mixed cues stay unresolved.
        different = any(scope not in line for scope in explicit_scopes for line in conditional_scopes)
        return ("neutral",None,comparable) if different else ("unknown","scope",comparable)
    if len(decisions)==1:
        return next(iter(decisions)),None,comparable
    return "unknown",("uncovered" if "uncovered" in flags or not brand.search(clean(text)) else "description"),comparable


def sidecar(snapshot):
    return snapshot.with_suffix(".recommendation-review.json")


def identity(snapshot):
    return hashlib.sha256(snapshot.read_bytes()).hexdigest()


def saved_review(snapshot, *, previous=False):
    path = sidecar(snapshot)
    if not path.is_file():
        return None
    data = api.read_json(path.read_bytes())
    versions = (VERSION,"recommendation-review/1") if previous else (VERSION,)
    if data.get("snapshot") != identity(snapshot) or data.get("version") not in versions:
        return None
    api.require(isinstance(data.get("records"),dict) and type(data.get("total")) is int,
                "推荐复检 / 本地结果结构异常")
    for row in data["records"].values():
        api.require(isinstance(row,dict) and row.get("status") in STATUSES and
                    (row["status"]!="ok" or (row.get("label") in RESULTS and
                     type(row.get("table")) is bool and
                     ((row["label"]=="unknown" and row.get("reason") in REASONS) or
                      (row["label"]!="unknown" and row.get("reason") is None)))),
                    "推荐复检 / 分类结果结构异常")
    return data


def review_result(body,prompt,market):
    label,reason,table = classify(body,prompt,market)
    return {"status":"ok","label":label,"reason":reason,"table":table}


def write_review(snapshot,digest,records):
    api.require(identity(snapshot)==digest,"推荐复检 / 复检期间快照已变化，请重跑")
    data = {"snapshot":digest,"version":VERSION,"total":len(records),"records":records,
            "checked_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}
    api.write_atomic(sidecar(snapshot),json.dumps(data,ensure_ascii=False).encode())


def run_review(bank,snapshot,brand,call,progress=None,*,defer_unavailable=False):
    sha,_,by_text = api.load_bank(bank)
    prompts = {q:t for t,ids in by_text.items() for q in ids}
    digest = identity(snapshot)
    with closing(sqlite3.connect(snapshot.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        batch = dict(db.execute("SELECT * FROM batch").fetchone())
        api.require(batch["question_version"]==sha,"推荐复检 / 题库版本不一致")
        rows = db.execute("""
            SELECT s.task_id,s.question_id,s.task_date,s.platform,s.language,c.ranks,q.market
            FROM sample s JOIN sample_content c USING(task_id) JOIN question q USING(question_id)
            WHERE s.status='valid' AND c.recommendation='unknown' ORDER BY s.task_id
        """).fetchall()
    past = saved_review(snapshot,previous=True)
    records = {}
    if past:
        for r in rows:
            task = r["task_id"]
            old = past["records"].get(task,{})
            if old.get("status")=="ok" and past["version"]==VERSION:
                records[task] = old
            elif defer_unavailable and old.get("status") in ("read_failed","task_missing","deferred"):
                records[task] = {"status":"deferred"}
    remaining = [r for r in rows if r["task_id"] not in records]

    def inspect(row):
        try:
            detail = call(api.DETAIL,{"brand_id":brand,"task_id":row["task_id"]})
        except api.ArchiveError as error:
            return {"status":"task_missing" if "（106001）" in str(error) else "read_failed"}
        body, prompt = detail.get("llm_output"), detail.get("query") or detail.get("prompt")
        state = api.analysis_state(detail)
        if (not isinstance(body,str) or not body.strip() or not isinstance(prompt,str)
            or api.question_text(prompt)!=prompts.get(row["question_id"])
            or detail.get("date")!=row["task_date"]
            or any(detail.get(f)!=row[f] for f in ("platform","language"))
            or (state is not None and state!=2)):
            return {"status":"mismatch"}
        _,ranks,rec,_,_=api.content_flags(body,prompt,row["market"],detail.get("brand_reputation"))
        if ranks!=json.loads(row["ranks"]) or rec!="unknown":
            return {"status":"mismatch"}
        return review_result(body,prompt,row["market"])

    with ThreadPoolExecutor(max_workers=5) as pool:
        for i,(row,result) in enumerate(zip(remaining,pool.map(inspect,remaining)),1):
            records[row["task_id"]] = result
            if progress and i%200==0:
                progress(len(records),len(rows))
    api.require(len(records)==len(rows),"推荐复检 / 任务总数未闭合")
    write_review(snapshot,digest,records)
    return Counter(r["status"] for r in records.values())
