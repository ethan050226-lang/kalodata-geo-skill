"""Local answer screening; excluded answers remain available for verification."""
from collections import defaultdict
import json
import re
from statistics import median
from unicodedata import normalize

VERSION = 'answer-quality/2'
# Heuristics flag anomalies, not a verified cause or a model-quality score.
MIN_REFERENCE = 200
MIN_DROP = 150
MAX_RATIO = .35
UNITS = re.compile(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0e00-\u0e7f]|[^\W_]+', re.UNICODE)
ERROR = re.compile(r'(?:error\s*:\s*)?(?:HTTP\s*[45]\d\d(?:\s+(?:service unavailable|bad gateway|'
                   r'internal server error|not found|forbidden|too many requests))?|internal server error|'
                   r'bad gateway|service unavailable|request (?:failed|timed out)(?:\s*:\s*HTTP\s*[45]\d\d)?|'
                   r'rate limit(?:ed| exceeded)?|too many requests|something went wrong|'
                   r'(?:请求|采集|抓取|回答|生成|网络|服务)(?:失败|错误|异常|超时)|系统繁忙|访问被拒绝|验证码|'
                   r'(?:糟糕[，,\s]*)?出了点问题)'
                   r'(?:[：:。！!,.\s]*(?:please )?(?:try again(?: later)?|请稍后重试|稍后重试))?[。！!,.\s]*', re.I)
REFUSAL = re.compile(normalize('NFKC',r'(?:(?:(?:i(?:\x27m| am) )?sorry[, .!]*)?i (?:cannot|can\x27t|am unable to) '
                     r'(?:(?:answer|respond to|help with) (?:that|this|your|the) (?:question|request)|'
                     r'provide (?:that information|this information|an answer|the requested information))|'
                     r'(?:抱歉|对不起|很抱歉)[，,。\s]*(?:我)?(?:无法|不能)(?:为您)?(?:回答|处理|提供帮助)(?:这个问题|该问题|此问题|您的问题|此请求)?'
                     r'|(?:申し訳ありませんが[、,\s]*)?(?:この質問には|それには)回答できません'
                     r'|(?:죄송하지만\s*)?이 질문에 답변할 수 없습니다'
                     r'|(?:leider\s+)?(?:kann ich diese Anfrage nicht beantworten|kann ich diese Frage nicht beantworten)'
                     r'|(?:désolé[e]?[,\s]*)?je ne peux pas répondre à (?:cette demande|cette question|cela)'
                     r'|(?:xin lỗi[,\s]*)?tôi không thể trả lời câu hỏi này'
                     r'|(?:ขออภัย\s*)?ฉันไม่สามารถตอบคำถามนี้ได้'
                     r'|(?:maaf[,\s]*)?saya tidak (?:dapat menjawab pertanyaan ini|dapat menjawab soalan ini|boleh menjawab soalan ini)'
                     r')[。.!\s]*'), re.I)
TRUNCATED = re.compile(r'(?:\[(?:回答|内容|输出)(?:已)?截断\]|\[(?:response|output) truncated\]|'
                       r'\[(?:generation|response) interrupted\])\s*$', re.I)
# Only explicit, mechanically checkable requests support automatic omission exclusion.
REQUIREMENTS = (
    ('限制说明', re.compile(r'\b(?:limitations?|caveats?)\b|局限|限制|注意事项', re.I),
     re.compile(r'\b(?:limitations?|caveats?|estimat\w*|verify|verification)\b|局限|限制|估算|参考|核实', re.I)),
    ('来源链接', re.compile(r'\b(?:provide|include|give|cite)\b[^.!?\n]{0,60}\b(?:sources?|links?|references?)\b|'
                        r'(?:提供|附上|给出|列出)[^。！？\n]{0,20}(?:来源|链接|出处)', re.I),
     re.compile(r'https?://|www\.', re.I)),
)


def error_envelope(text):
    if len(text) > 16000:
        return False
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```',text,re.I|re.S)
        if not match:
            return False
        text = match[1]
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return False
    return (isinstance(data,dict) and bool(data.get('error')) and
            set(data) <= {'error','message','detail','code','status','request_id','timestamp','success','data'} and
            data.get('data') in (None, '', [], {}))


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
                  '报错或采集失败文本' if error_envelope(text) or units <= 80 and ERROR.fullmatch(plain) else
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
        requested = [(label,body) for label,prompt,body in REQUIREMENTS if prompt.search(rows[0]['prompt'])]
        complete = [r for r in rows if len(requested)>=2 and
                    all(pattern.search(r['body']) for _,pattern in requested)]
        # A majority of incomplete short peers must not become the completeness reference.
        if complete:
            baseline = max(results[r['task']]['units'] for r in complete)
        if baseline < MIN_REFERENCE:
            continue
        reference = min(complete or rows, key=lambda r:(abs(results[r['task']]['units']-baseline), str(r['task'])))
        reference_units = results[reference['task']]['units']
        for row in rows:
            item = results[row['task']]
            ratio = item['units'] / reference_units
            item.update(reference_task=reference['task'],reference_units=reference_units,ratio=ratio)
            if ratio <= MAX_RATIO and reference_units-item['units'] >= MIN_DROP:
                missing = [label for label,pattern in requested if not pattern.search(row['body'])]
                if complete and len(missing)>=2:
                    item.update(status='shrunken', reason='明显变短且未检出题目要求的'+ '、'.join(missing)+'（规则初判）')
                else:
                    item['reason']='长度明显下降；暂未证实内容缺失，仍纳入统计'
    return results
