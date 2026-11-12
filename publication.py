"""Versioned, fail-closed Feishu publication; no Answerbit or Feishu secrets here."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from archive import require, write_atomic
from recommendation_review import VERSION as REVIEW_VERSION

SCHEMA = 'geo-publication/1'
CHECKS = ('market', 'platform', 'date', 'group', 'tag', 'matrix', 'navigation',
          'comparison', 'charts', 'metric_guide', 'desktop', 'narrow_screen', 'permissions')


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def manifest(snapshot, data):
    """Full source hashes stay local; public manifests contain only checksums and scope."""
    from dashboard import ASSET
    with closing(sqlite3.connect(snapshot.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'sample', 'question', 'sample_answer', 'sample_question', 'sample_group',
                    'sample_tag', 'sample_website', 'sample_content', 'issue', 'retrieval_scope', 'source_evidence'}
        require(required <= tables, '版本校验 / 历史批次缺少完整来源校验信息，不能判为相同版本；请重新取数')
        db.row_factory = sqlite3.Row
        batch = dict(db.execute('SELECT * FROM batch').fetchone())
        require(batch == data['batch'], '版本校验 / 看板和数据库不是同一批次')
        source = dict(db.execute('SELECT * FROM source_evidence').fetchone())
        require(source.get('schema') == SCHEMA and source.get('complete') == 1,
                '版本校验 / 来源指纹不完整')
        ids = sorted(r[0] for r in db.execute('SELECT task_id FROM sample UNION SELECT task_id FROM issue'))
        require(len(ids) == batch['returned_records'], '版本校验 / 返回样本 ID 集合与完整批次数量不符')
        content = {}
        for table in sorted(required - {'source_evidence'}):
            rows = [dict(r) for r in db.execute(f'SELECT * FROM {table}')]
            content[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
        if 'delivery_scope' in tables:
            from market_scope import validate_data_scope
            markets = json.loads(db.execute('SELECT markets FROM delivery_scope').fetchone()[0])
            require(data.get('market_scope',{}).get('markets') == markets, '国家范围 / 看板和数据库范围不同')
            require({r[0] for r in db.execute('SELECT DISTINCT market FROM question')} <= set(markets),
                    '国家范围 / 数据库仍含范围外国家')
            validate_data_scope(data)
            content['delivery_scope'] = markets
        require(db.execute("SELECT count(*) FROM sample WHERE status='valid'").fetchone()[0]
                == sum(r['n'] for r in data['rows']), '版本校验 / 有效样本量不一致')
        require(not db.execute("SELECT count(*) FROM issue WHERE coalesce(question_id,'')='' AND coalesce(candidate_question_ids,'')<>''").fetchone()[0],
                '版本校验 / 题库候选编号尚未确认')
    scope = sorted({json.dumps(r['d'], ensure_ascii=False, sort_keys=True) for r in data['rows']})
    stable = {k: v for k, v in data.items() if k not in ('batch', 'comparison', 'bank_source')}
    if stable.get('platform'):
        stable['platform'] = {k: v for k, v in stable['platform'].items() if k not in ('fetched', 'fetched_at_utc', 'read_at')}
    # Review records contain judgments only; run timestamps must not create false versions.
    if stable.get('review'):
        stable['review'] = {k: v for k, v in stable['review'].items() if k != 'checked_at'}
    result = {'schema': SCHEMA, 'window': [batch['begin_date'], batch['end_date']],
              'brand_sha256': source['brand_sha256'], 'bank_sha256': batch['question_version'],
              'metric_version': batch['metric_version'], 'analysis_version': data['analysis_version'],
              'review_version': REVIEW_VERSION, 'scope_sha256': digest(scope), 'scope': scope,
              'sample_ids_sha256': digest(ids), 'sample_count': len(ids),
              'source_sha256': source['content_sha256'], 'computed_sha256': digest(content),
              'dashboard_sha256': digest(stable), 'template_sha256': hashlib.sha256(ASSET.read_bytes()).hexdigest()}
    result['version'] = digest(result)
    return result


def report(data):
    from dashboard import report_metrics
    metrics = report_metrics(data)
    primary = [m for m in metrics if m['id'] in ('skill.core_mention_rate','skill.first_rate','skill.adverse_rate')]
    comparison = data.get('comparison') or {}
    compared = {m['id']:m for m in comparison.get('metrics',[])}
    for metric in primary:
        old = compared.get(metric['id'],{})
        metric.update(previous=old.get('previous'), change=old.get('change') if not comparison.get('limitations') else None)
    actions = []
    plans = {'absence': ('核对未点名回答是否遗漏品牌或存在明确负面', '补充对应场景选型和适用人群内容', '提及率'),
             'formal': ('核对回答正式来源是否支持品牌描述', '补充有证据、可引用的官网说明页', '官网正式来源命中率'),
             'sentiment': ('核对平台负面或褒贬并存的具体说法', '核实后修正相关能力、限制或价格说明', '负面与褒贬并存占比')}
    for signal, (check, direction, metric) in plans.items():
        candidates = [r for r in data['rows'] if signal in r.get('evidence', {})]
        if not candidates: continue
        row = max(candidates, key=lambda r:(r.get('evidence_counts',{}).get(signal,0),r['n'])); ref = row['evidence'][signal]
        actions.append({'priority': f'P{len(actions)+1}', 'fact': check, 'suggestion': direction,
                        'sample_count': row['n'], 'affected_count':row.get('evidence_counts',{}).get(signal), 'reference': ref,
                        'verify': 'Answerbit 工作台 → 大模型回答记录，按题文、平台、日期和分组查找；题号用于飞书题库',
                        'retest': f'同题、同平台和同等窗口复测{metric}，范围构成保持一致'})
    actions.sort(key=lambda a:(a['affected_count'] or 0,a['sample_count']),reverse=True)
    for i,action in enumerate(actions,1): action['priority']=f'P{i}'
    core = next(m for m in primary if m['id'] == 'skill.core_mention_rate')
    value = f"{core['value']:.2f}%（{core['numerator']}/{core['denominator']}）" if core['value'] is not None else '不适用（没有可用分母或缺少口径证据）'
    conclusion = f"本批 {sum(r['n'] for r in data['rows'])} 条有效回答，提及率为{value}。"
    gaps = [r for r in data['rows'] if not r['d'][4] and 'absence' in r.get('evidence',{})]
    if gaps:
        row = max(gaps,key=lambda r:r['n']-(r['mentions'].get('Kalodata',0)-r.get('negative_mentions',0)))
        m, platform, day, group = row['d'][:4]
        num = row['mentions'].get('Kalodata',0)-row.get('negative_mentions',0)
        conclusion += (f"优先回查 {data['markets'].get(m,m)} / {data['groups'].get(group,'分组未知')} / {platform} / {day}，"
                       f"该切片为 {num}/{row['n']}，存在未提及或明确负面的代表回答。")
    conclusion += '各切片样本构成可能不同；内容优化是待复测的建议，不据此推断因果或整体市场优劣。'
    return {'batch': data['batch'], 'metrics': primary, 'actions': actions,
            'comparison': {k:comparison.get(k) for k in ('previous_batch','current_batch','limitations')},
            'conclusion': conclusion,
            'limitations': {'issues': data.get('issues'), 'sampling_coverage': '缺少独立计划量，无法计算',
                            'recommendation': '本地规则初判，不等于人工确认',
                            'unknown_reputation': sum(r['sent'].get('unknown', 0) for r in data['rows'])}}


def valid_receipt(receipt):
    require(isinstance(receipt,dict) and isinstance(receipt.get('manifest'),dict)
            and isinstance(receipt.get('proof'),dict) and isinstance(receipt.get('report'),dict),
            '飞书查重 / 旧清单缺少完整版本或验收对象')
    required = {'schema','window','brand_sha256','bank_sha256','metric_version','analysis_version',
                'review_version','scope_sha256','scope','sample_ids_sha256','sample_count',
                'source_sha256','computed_sha256','dashboard_sha256','template_sha256','version'}
    require(receipt.get('schema') == SCHEMA and receipt.get('accepted') is True
            and set(receipt.get('manifest', {})) == required and receipt.get('html_sha256')
            and receipt.get('file_token') and receipt.get('url') and receipt.get('accepted_at')
            and receipt.get('report',{}).get('batch'), '飞书查重 / 已验收记录缺少完整校验信息')
    proof = receipt.get('proof', {})
    require(set(proof) == {'file_token','html_sha256','checks','account','checked_at','access_scope'},
            '飞书验收 / 验收证据包含未审核字段')
    from feishu_delivery import reject_secrets
    reject_secrets(receipt)
    require(isinstance(proof.get('checks'),dict) and proof.get('file_token') == receipt['file_token']
            and proof.get('html_sha256') == receipt['html_sha256']
            and all(proof.get('checks', {}).get(k) is True for k in CHECKS)
            and proof.get('account') and proof.get('checked_at')
            and proof.get('access_scope') in ('admins_only','operators_verified'), '飞书验收 / 缺少逐版本交互和账号验收证据')
    expected = dict(receipt['manifest']); version = expected.pop('version')
    require(version == digest(expected), '飞书查重 / 版本清单校验失败')


def lookup(remote, current):
    """Read the complete remote catalogue before any write. Never use names as equality proof."""
    files = remote.list_files()
    records, legacy = [], []
    for file in files:
        if file['name'].startswith('geo-accepted-') and file['name'].endswith('.json'):
            receipt = json.loads(remote.download(file['token']))
            valid_receipt(receipt)
            records.append(receipt)
        elif file['name'].endswith('.html') and not file['name'].startswith('geo-pending-'):
            legacy.append(file['name'])
    matches = [r for r in records if r['manifest'] == current]
    for found in sorted(matches, key=lambda r:r['accepted_at'], reverse=True):
        require(found['url'] == remote.url(found['file_token']), '飞书查重 / 链接与文件标识不一致')
        remote.verify_file(found['file_token'])
        require(hashlib.sha256(remote.download(found['file_token'])).hexdigest() == found['html_sha256'],
                '飞书查重 / 旧链接文件内容已变化，不得复用')
        return found, records, legacy
    return None, records, legacy


def stage(remote, data, version, html, state):
    found, records, legacy = lookup(remote, version)
    countries = lambda m: {json.loads(s)[0] for s in m['scope']}
    records = [r for r in records if countries(r['manifest']) == countries(version)]
    if found:
        write_atomic(state, json.dumps(found, ensure_ascii=False,sort_keys=True).encode())
        return {'status':'reused', 'receipt':found, 'legacy_files':legacy,
                'checked_at':datetime.now(timezone.utc).isoformat()}
    if records:
        previous = max(records,key=lambda r:r['accepted_at'])
        remote.verify_file(previous['file_token'])
        require(hashlib.sha256(remote.download(previous['file_token'])).hexdigest() == previous['html_sha256'],
                '飞书查重 / 上一已验收文件发生变化，请核查后再发布')
        write_atomic(state,json.dumps(previous,ensure_ascii=False,sort_keys=True).encode())
    token = remote.upload_private(html, 'geo-pending-'+version['version']+'.html')
    remote.verify_file(token)
    sha = hashlib.sha256(html).hexdigest()
    require(hashlib.sha256(remote.download(token)).hexdigest() == sha, '飞书完整性 / 上传后内容校验失败')
    return {'status':'pending_acceptance', 'file_token':token, 'url':remote.url(token),
            'manifest':version, 'html_sha256':sha, 'report':report(data), 'legacy_files':legacy,
            'previous': max(records,key=lambda r:r['accepted_at']) if records else None}


def accept(remote, candidate, proof, state):
    require(candidate.get('status') == 'pending_acceptance', '飞书验收 / 没有待验收版本')
    receipt = {k:candidate[k] for k in ('manifest','file_token','url','html_sha256','report')}
    receipt.update(schema=SCHEMA, accepted=True, accepted_at=datetime.now(timezone.utc).isoformat(), proof=proof)
    valid_receipt(receipt)
    require(receipt['url'] == remote.url(receipt['file_token']), '飞书验收 / 链接与文件标识不一致')
    remote.verify_file(receipt['file_token'])
    require(hashlib.sha256(remote.download(receipt['file_token'])).hexdigest() == receipt['html_sha256'],
            '飞书验收 / 验收期间文件变化，保留上次入口')
    body = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
    token = remote.upload_private(body, 'geo-accepted-'+candidate['manifest']['version']+'.json')
    remote.verify_file(token)
    require(remote.download(token) == body, '飞书验收 / 已验收记录回读失败，未切换入口')
    write_atomic(state, body)
    return {'status':'published', 'receipt':receipt}
