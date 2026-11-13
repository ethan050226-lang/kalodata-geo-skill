"""Local answer screening; excluded answers remain available for verification."""
from collections import defaultdict
import re
from statistics import median
from unicodedata import normalize

VERSION = 'answer-quality/1'
# Heuristics flag anomalies, not a verified cause or a model-quality score.
MIN_REFERENCE = 200
MIN_DROP = 150
MAX_RATIO = .35
UNITS = re.compile(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0e00-\u0e7f]|[^\W_]+', re.UNICODE)
ERROR = re.compile(r'^(?:error\s*:|HTTP\s*[45]\d\d\b|(?:internal server error|bad gateway|service unavailable|'
                   r'request (?:failed|timed out)|rate limit(?:ed| exceeded)?|too many requests)\b|'
                   r'(?:请求|采集|抓取|回答|生成|网络|服务)(?:失败|错误|异常|超时)(?:[：:。！,.\s]|$)|系统繁忙|访问被拒绝|验证码)', re.I)
REFUSAL = re.compile(r'(?:(?:(?:i(?:\x27m| am) )?sorry[, .!]*)?i (?:cannot|can\x27t|am unable to) '
                     r'(?:(?:answer|respond to|help with) (?:that|this|your|the) (?:question|request)|'
                     r'provide (?:that information|this information|an answer|the requested information))|'
                     r'(?:抱歉|对不起|很抱歉)[，,。\s]*(?:我)?(?:无法|不能)(?:为您)?(?:回答|处理|提供帮助)(?:这个问题|该问题|此问题|您的问题|此请求)?'
                     r')[。.!\s]*', re.I)
TRUNCATED = re.compile(r'(?:\[(?:回答|内容|输出)(?:已)?截断\]|\[(?:response|output) truncated\]|'
                       r'\[(?:generation|response) interrupted\])\s*$', re.I)


def text_units(body):
    return len(UNITS.findall(normalize('NFKC', body or '')))


def assess(records):
    """Records have task, prompt, body and a comparable question/platform/config key."""
    results, groups = {}, defaultdict(list)
    for row in records:
        body = row['body'] if isinstance(row['body'], str) else ''
        text = normalize('NFKC', body).strip()
        plain = re.sub(r'^[#>*\s]+', '', text)
        units = text_units(text)
        echo = re.sub(r'[\W_]+', '', text).casefold()
        prompt = re.sub(r'[\W_]+', '', normalize('NFKC', row['prompt'])).casefold()
        reason = ('空白回答' if not text else '仅复述题文' if echo == prompt else
                  '报错或采集失败文本' if units <= 80 and ERROR.search(plain) else
                  '仅返回拒答或无法作答' if units <= 60 and REFUSAL.fullmatch(plain) else
                  '明确的回答截断标记' if TRUNCATED.search(text) else None)
        results[row['task']] = dict(status='invalid' if reason else 'accepted', reason=reason, units=units,
                                    reference_task=None, reference_units=None, ratio=None, version=VERSION)
        if not reason:
            groups[row['key']].append(row)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        lengths = [results[r['task']]['units'] for r in rows]
        baseline = max(lengths) if len(rows) == 2 else median(lengths)
        if baseline < MIN_REFERENCE:
            continue
        reference = min(rows, key=lambda r:(abs(results[r['task']]['units']-baseline), str(r['task'])))
        reference_units = results[reference['task']]['units']
        for row in rows:
            item = results[row['task']]
            ratio = item['units'] / reference_units
            item.update(reference_task=reference['task'],reference_units=reference_units,ratio=ratio)
            if ratio <= MAX_RATIO and reference_units-item['units'] >= MIN_DROP:
                item.update(status='shrunken', reason='同题同模型回答明显缩水（本地规则初判）',
                            reference_task=reference['task'], reference_units=reference_units, ratio=ratio)
    return results
