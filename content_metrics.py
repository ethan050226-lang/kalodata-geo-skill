"""Classify transient Answerbit text and aggregate structured GEO measurements."""
from collections import Counter, defaultdict
from functools import lru_cache
import re
from unicodedata import normalize


KALODATA = "Kalodata"
COMMON = ("FastMoss", "EchoTik")
COMPETITORS = {
    "US": COMMON + ("Winning Hunter",),
    "JP": COMMON + ("Astream Deep", "Exolyt", "TikTok Studio", "Modash", "Upfluence"),
    "CN": COMMON + ("出海匠",),
    "KR": COMMON + ("出海匠",),
}
ALIASES = {
    KALODATA: (r"kalodata", r"kalo[\s-]+data", r"カロデータ"),
    "FastMoss": (r"fast[\s-]*moss",),
    "EchoTik": (r"echo[\s-]*tik",),
    "Winning Hunter": (r"winning[\s-]*hunter",),
    "Astream Deep": (r"astream[\s-]+deep",),
}
RECOMMENDATIONS = ("explicit", "conditional", "neutral", "no_recommendation", "unknown")
SENTIMENTS = ("positive", "neutral", "negative", "mixed", "unknown")
# shortcut: undocumented values remain unmapped until matched with the same
# answer's platform label or an authoritative enum.
VERIFIED_REPUTATION = {}


def market_brands(market):
    return (KALODATA, *COMPETITORS.get(market, COMMON))


@lru_cache(maxsize=None)
def brand_patterns(market):
    patterns = {}
    for name in market_brands(market):
        aliases = ALIASES.get(name, (re.escape(name).replace(r"\ ", r"\s+"),))
        parts = [r"(?<![a-z0-9_])(?:" + a + r")(?![a-z0-9_])"
                 if a.isascii() else a for a in aliases]
        patterns[name] = re.compile("|".join(parts), re.I)
    return patterns


def strip_opening_echo(body, question):
    """Strip one literal question at the start, allowing markdown and whitespace."""
    body, question = normalize("NFKC", body), normalize("NFKC", question).strip()
    if not question:
        return body
    words = r"\s+".join(map(re.escape, question.split()))
    # A continuing word means this is a prefix, not a complete echoed question.
    prefix = r"^\s*[>#*_`\s]*(?:(?:question|prompt|q|问题|提问|質問)\s*[:：]\s*)?[*_`「『“\s]*"
    boundary = "" if question[-1] in "!?。？！" else r"(?=$|[\s*_`」』”！？?。:：])"
    match = re.match(prefix + words + boundary, body, re.I)
    return body[match.end():] if match else body


def first_ranks(text, market):
    positions = [(m.start(), name) for name, pattern in brand_patterns(market).items()
                 if (m := pattern.search(text))]
    return {name: rank for rank, (_, name) in enumerate(sorted(positions), 1)}


CUE = re.compile(
    r"\b(?:recommend(?:s|ed)?|suggest(?:s|ed)?|should (?:use|choose|try)|"
    r"(?:best|top|first) (?:choice|pick|option)|"
    r"(?:can|could|may|consider) (?:use|choose|try))\b|"
    r"推荐|推薦|建议|建議|首选|首選|优先选|優先選|可以(?:选|選|用)|おすすめ|お勧め|推奨|選ぶべき",
    re.I,
)
WEAK = re.compile(r"\b(?:can|could|may|consider)\b|可以", re.I)
CONDITION = re.compile(
    r"\b(?:if|only when|only for|provided that|as long as|unless|"
    r"for (?:teams|users|sellers|agencies|businesses|beginners|creators) (?:who|with|that))\b|"
    r"如果|假如|若(?:你|您|是|要|有|需)|只有|只在|仅在|僅在|仅当|僅當|前提|なら|場合|とき|条件",
    re.I,
)
NEGATION = re.compile(
    r"\b(?:do(?:es)? not|don't|doesn't|would not|wouldn't|not recommend(?:ed)?|"
    r"never|avoid|cannot|can't|should not|shouldn't)\b|"
    r"不(?:太)?(?:推荐|推薦|建议|建議)|不宜|不要|不适合|不適合|おすすめし(?:ない|ません)|非推奨",
    re.I,
)
REPORTED = re.compile(r"\b(?:according to|users say|people say|reviews say|they recommend)\b|据称|據稱|有人推荐|有人推薦|によると", re.I)
HEDGE = re.compile(r"\b(?:might|possibly|not sure|unclear|it depends|whether)\b|不确定|不確定|也许|也許|かもしれ", re.I)
SPLIT = re.compile(r"(?<=[.!?。！？])\s*|[;；\n]+|\s+(?:but|however|whereas)\s+|[，,]\s*(?:但(?:是)?|不过|不過)")


def recommendation(text, market, ranks):
    if KALODATA not in ranks:
        return "no_recommendation"
    patterns = brand_patterns(market)
    found, uncertain, negative = set(), False, False
    for clause in SPLIT.split(text):
        if not patterns[KALODATA].search(clause):
            if found and CUE.search(clause) and CONDITION.search(clause):
                uncertain = True
            continue
        cues = list(CUE.finditer(clause))
        if not cues:
            continue
        # shortcut: require an explicit local target and cue; leave unresolved
        # cross-sentence references, quotations and competing targets unknown.
        if (REPORTED.search(clause) or HEDGE.search(clause)
                or any(pattern.search(clause) for brand, pattern in patterns.items() if brand != KALODATA)
                or re.search(r'["“”「」『』]', clause)):
            uncertain = True
            continue
        if NEGATION.search(clause):
            if len(cues) != 1 or CONDITION.search(clause):
                uncertain = True
            else:
                negative = True
            continue
        if CONDITION.search(clause):
            found.add("conditional")
        elif any(not WEAK.search(cue.group()) for cue in cues):
            found.add("explicit")
        else:
            uncertain = True
    if uncertain or (negative and found):
        return "unknown"
    if len(found) == 2:
        return "neutral"
    return next(iter(found), "no_recommendation" if negative else "unknown")


def summarize_content(rows, tags_by_task):
    buckets = defaultdict(list)
    neutral = defaultdict(list)
    for row in rows:
        market, named = row["market"], row["named_prompt"]
        keys = [("market", market, named, ""),
                ("group", market, named, row["group_id"] or ""),
                ("platform", market, named, row["platform"]),
                ("question", market, named, row["question_id"]),
                ("group_platform", market, named, (row["group_id"] or "", row["platform"]))]
        keys.extend(("tag", market, named, tag) for tag in tags_by_task[row["task_id"]])
        for key in keys:
            buckets[key].append(row)
        if row["recommendation"] == "neutral":
            neutral[row["question_id"]].append({key: row[key] for key in
                ("task_id", "task_date", "platform", "group_name", "market", "prompt")})
    summaries = defaultdict(list)
    for (dimension, market, named, value), items in sorted(buckets.items()):
        mention_counts = Counter(brand for item in items for brand in item["ranks"])
        valid, sov_denominator = len(items), sum(mention_counts.values())
        brands = []
        for brand in market_brands(market):
            hits = mention_counts[brand]
            brands.append(dict(brand=brand, mentions=hits, mention_rate=hits / valid,
                               sov=hits / sov_denominator if sov_denominator else None))
        ranks = [item["ranks"][KALODATA] for item in items if KALODATA in item["ranks"]]
        first = ranks.count(1)
        summaries[dimension].append(dict(market=market, named_prompt=named, value=value, valid=valid,
            brands=brands, sov_denominator=sov_denominator, first=first, first_rate=first / valid,
            rank_sum=sum(ranks), rank_count=len(ranks), average_rank=sum(ranks) / len(ranks) if ranks else None))
    rec = Counter(row["recommendation"] for row in rows)
    sentiment = Counter(row["sentiment"] for row in rows)
    raw = Counter(str(row["reputation_code"]) if row["reputation_code"] is not None else "missing" for row in rows)
    valid = len(rows)
    return dict(valid=valid, dimensions=dict(summaries),
                recommendation={key: rec[key] for key in RECOMMENDATIONS},
                recommendation_known=valid - rec["unknown"],
                recommendation_rates={key: rec[key] / valid if valid else None for key in RECOMMENDATIONS},
                sentiment={key: sentiment[key] for key in SENTIMENTS},
                sentiment_known=valid - sentiment["unknown"], sentiment_raw_counts=dict(raw),
                neutral_questions=[dict(question_id=qid, label="中立推荐", count=len(items), answers=items)
                                   for qid, items in sorted(neutral.items())])
