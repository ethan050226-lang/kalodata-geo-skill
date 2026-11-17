"""Versioned, fail-closed Feishu publication; no Answerbit or Feishu secrets here."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from archive import ArchiveError, require, write_atomic
from recommendation_review import VERSION as REVIEW_VERSION

SCHEMA = 'geo-publication/1'
REGISTRY_SCHEMA = 'geo-publication-registry/1'
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
        if data.get('quality') is not None:
            required.add('sample_quality')
        if data.get('model_policy'):
            required.add('country_model_audit')
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
            if 'model_policy' in {r[1] for r in db.execute('PRAGMA table_info(delivery_scope)')}:
                content['model_policy']=db.execute('SELECT model_policy FROM delivery_scope').fetchone()[0]
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
    from market_scope import sampling_plan
    metrics = report_metrics(data)
    primary = [m for m in metrics if m['id'] in ('skill.core_mention_rate','skill.first_rate','skill.adverse_rate')]
    comparison = data.get('comparison') or {}
    compared = {m['id']:m for m in comparison.get('metrics',[])}
    for metric in primary:
        old = compared.get(metric['id'],{})
        metric.update(previous=old.get('previous'), change=old.get('change') if not comparison.get('limitations') else None)
    actions = []
    plans = {'absence': ('未点名回答未提及品牌或为明确负面', '确认选型说明缺失后，补充适用场景及人群', '提及率'),
             'formal': ('已提及品牌的回答未命中官网正式来源', '确认官网说明缺失后，补充对应语言的功能和步骤', '官网正式来源命中率'),
             'sentiment': ('平台口碑为负面或褒贬并存', '确认说法过时或有误后，更新对应功能、限制或价格说明', '负面与褒贬并存占比')}
    for signal, (check, direction, metric) in plans.items():
        candidates = [r for r in data['rows'] if signal in r.get('evidence', {})]
        if not candidates: continue
        row = max(candidates, key=lambda r:(r.get('evidence_counts',{}).get(signal,0),r['n'])); ref = row['evidence'][signal]
        affected=row.get('evidence_counts',{}).get(signal)
        location=' / '.join((data['markets'].get(row['d'][0],row['d'][0]),data['groups'].get(row['d'][3],'分组未知'),row['d'][1]))
        fact=f'{location}：{affected} 条{check}' if affected is not None else f'{location}：{ref["q"]} 对应回答待核查'
        actions.append({'priority': f'P{len(actions)+1}', 'status':'待核查','fact':fact, 'suggestion': direction,
                        'sample_count': row['n'], 'affected_count':row.get('evidence_counts',{}).get(signal), 'reference': ref,
                        'verify': 'Answerbit 工作台 → 大模型回答记录，按题文、平台、日期和分组查找；题号用于飞书题库',
                        'retest': f'同题、同平台、同窗口复测{metric}'})
    actions.sort(key=lambda a:(a['affected_count'] or 0,a['sample_count']),reverse=True)
    for i,action in enumerate(actions,1): action['priority']=f'P{i}'
    core = next(m for m in primary if m['id'] == 'skill.core_mention_rate')
    value = f"{core['value']:.2f}%（{core['numerator']}/{core['denominator']}）" if core['value'] is not None else '不适用（没有可用分母或缺少口径证据）'
    conclusion = f"本批 {sum(r['n'] for r in data['rows'])} 条有效回答，提及率为{value}。"
    if data.get('core_findings'):
        from core_findings import validate
        findings=validate(data)
        entry=next(e for e in findings['entries'] if e['row_indices']==list(range(len(data['rows']))))
        conclusion=entry['conclusion']+(' 下一步：'+entry['judgment'] if entry['judgment'] else '')
    return {'batch': data['batch'], 'metrics': primary, 'actions': actions,
            'model_scope':{'models':data['model_policy']['models'],'version':data['model_policy']['version'],
                           'signature':data['model_policy']['signature'],
                           'excluded':len(data['model_policy']['excluded_records'])} if data.get('model_policy') else None,
            'sampling_requirements':sampling_plan(data['model_policy'],data['market_scope']['markets']) if data.get('model_policy') else None,
            'comparison': {k:comparison.get(k) for k in ('previous_batch','current_batch','limitations')},
            'conclusion': conclusion,
            'limitations': {'issues': data.get('issues'), 'sampling_coverage': '缺少独立计划量，无法计算',
                            'answer_quality': {'checked':data['quality']['checked'],
                                               'comparable':data['quality']['comparable'],
                                               'excluded':len(data['quality']['records']),
                                               'version':data['quality']['version']} if data.get('quality') else '旧批未检测，需刷新',
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
    sharing_scope = receipt.get('sharing_scope','admins_only')
    require(sharing_scope in ('admins_only','tenant_readable'), '飞书验收 / 未审核的分享范围')
    proof_fields = {'file_token','html_sha256','checks','account','checked_at','access_scope'}
    if sharing_scope == 'tenant_readable': proof_fields.add('operator')
    require(set(proof) == proof_fields,
            '飞书验收 / 验收证据包含未审核字段')
    from feishu_delivery import reject_secrets
    reject_secrets(receipt)
    require(isinstance(proof.get('checks'),dict) and proof.get('file_token') == receipt['file_token']
            and proof.get('html_sha256') == receipt['html_sha256']
            and all(proof.get('checks', {}).get(k) is True for k in CHECKS)
            and proof.get('account') and proof.get('checked_at')
            and proof.get('access_scope') in (('tenant_verified',) if sharing_scope == 'tenant_readable'
                                            else ('admins_only','operators_verified')),
            '飞书验收 / 缺少逐版本交互和账号验收证据')
    if sharing_scope == 'tenant_readable':
        operator = proof['operator']
        require(isinstance(operator,dict) and set(operator) == {'account','checked_at','checks','external_account','external_denied'}
                and operator.get('account') and operator['account'] != proof['account'] and operator.get('checked_at')
                and isinstance(operator.get('checks'),dict)
                and all(operator['checks'].get(k) is True for k in CHECKS)
                and operator.get('external_account') and operator['external_account'] not in (proof['account'],operator['account'])
                and operator.get('external_denied') is True,
                '飞书验收 / 公司内分享须由另一位真实运营账号测试交互，并验证公司外访问被拒绝')
    expected = dict(receipt['manifest']); version = expected.pop('version')
    require(version == digest(expected), '飞书查重 / 版本清单校验失败')


def read_registry(remote, state):
    """A missing ledger is unknown history, never an empty remote folder."""
    path = state.with_name('registry.json')
    try:
        registry = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ArchiveError('中央发布记录 / 缺失或损坏，停止发布；请管理员核对历史版本后初始化或恢复，不自动扫描目录或重新取数') from None
    require(isinstance(registry,dict) and registry.get('schema') == REGISTRY_SCHEMA
            and registry.get('target') == remote.registry_key
            and isinstance(registry.get('records'),list)
            and isinstance(registry.get('review'),dict)
            and registry['review'].get('history_checked') is True,
            '中央发布记录 / 格式或目标目录不符，停止发布，请管理员恢复')
    seen = set()
    for entry in registry['records']:
        require(isinstance(entry,dict) and set(entry) == {'receipt_token','receipt'}
                and isinstance(entry['receipt_token'],str) and entry['receipt_token'].isascii()
                and entry['receipt_token'].isalnum(),
                '中央发布记录 / 缺少已知回执文件标识')
        valid_receipt(entry['receipt'])
        receipt = entry['receipt']
        key = (receipt['manifest']['version'], receipt.get('sharing_scope','admins_only'))
        require(key not in seen, '中央发布记录 / 同版本记录冲突，请管理员核查')
        seen.add(key)
    if state.exists():
        try: previous = json.loads(state.read_text())
        except (OSError, ValueError):
            raise ArchiveError('中央发布记录 / 上次成功入口损坏，停止发布，请管理员恢复') from None
        require(any(e['receipt'] == previous for e in registry['records']),
                '中央发布记录 / 上次成功入口不在登记历史中，停止发布，请管理员恢复')
    return registry


def verify_registered(remote, entry):
    receipt = entry['receipt']
    remote.verify_file(entry['receipt_token'])
    try: saved = json.loads(remote.download(entry['receipt_token']))
    except ValueError:
        raise ArchiveError('中央发布记录 / 已知远端回执损坏，停止发布') from None
    require(saved == receipt, '中央发布记录 / 远端回执与登记记录不同，停止发布')
    require(receipt['url'] == remote.url(receipt['file_token']), '飞书查重 / 链接与文件标识不一致')
    remote.verify_file(receipt['file_token'],sharing_scope=receipt.get('sharing_scope','admins_only'))
    require(hashlib.sha256(remote.download(receipt['file_token'])).hexdigest() == receipt['html_sha256'],
            '飞书查重 / 已知文件内容已变化，停止发布，请核查')
    return receipt


def initialize_registry(remote, state, proof):
    """Admin-reviewed bootstrap/import by known receipt tokens; no directory enumeration."""
    path = state.with_name('registry.json')
    require(not path.exists(), '中央发布记录 / 已存在，不自动覆盖；损坏时请从批准备份恢复')
    require(isinstance(proof,dict) and set(proof) == {'account','checked_at','history_checked','receipt_tokens'}
            and isinstance(proof['account'],str) and proof['account'].strip()
            and isinstance(proof['checked_at'],str) and proof['checked_at'].strip()
            and proof['history_checked'] is True and isinstance(proof['receipt_tokens'],list)
            and all(isinstance(t,str) and t.isascii() and t.isalnum() for t in proof['receipt_tokens'])
            and len(set(proof['receipt_tokens'])) == len(proof['receipt_tokens']),
            '中央发布记录 / 需要管理员实际核对历史后的账号、时间及已知回执标识；未核查不能初始化为空')
    records = []
    seen = set()
    for token in proof['receipt_tokens']:
        remote.verify_file(token)
        try: receipt = json.loads(remote.download(token))
        except ValueError:
            raise ArchiveError('中央发布记录 / 待导入回执损坏，不初始化') from None
        valid_receipt(receipt)
        entry = {'receipt_token':token,'receipt':receipt}
        verify_registered(remote,entry)
        key = (receipt['manifest']['version'],receipt.get('sharing_scope','admins_only'))
        require(key not in seen, '中央发布记录 / 同版本导入冲突，请管理员核查')
        seen.add(key); records.append(entry)
    if state.exists():
        try: previous = json.loads(state.read_text())
        except (OSError, ValueError):
            raise ArchiveError('中央发布记录 / 上次成功入口损坏，请先恢复，不初始化为空') from None
        require(any(e['receipt'] == previous for e in records),
                '中央发布记录 / 历史导入遗漏上次成功入口，不初始化为空')
    registry = {'schema':REGISTRY_SCHEMA,'target':remote.registry_key,'review':proof,'records':records}
    write_atomic(path,json.dumps(registry,ensure_ascii=False,sort_keys=True).encode())
    return {'status':'registry_initialized','records':len(records),'publication':'未上传、未切换成功入口、未启用定时任务'}


def lookup(remote, current, state, sharing_scope='admins_only'):
    registry = read_registry(remote,state)
    entries = [e for e in registry['records'] if e['receipt']['manifest'] == current
               and e['receipt'].get('sharing_scope','admins_only') == sharing_scope]
    found = verify_registered(remote,entries[0]) if entries else None
    return found, registry


def stage(remote, data, version, html, state, sharing_scope='admins_only'):
    require(sharing_scope in ('admins_only','tenant_readable'), '飞书发布 / 未审核的分享范围')
    found, registry = lookup(remote, version,state,sharing_scope)
    legacy = None  # Not inspected; an unknown directory must not be reported as empty.
    countries = lambda m: {json.loads(s)[0] for s in m['scope']}
    entries = [e for e in registry['records'] if countries(e['receipt']['manifest']) == countries(version)
               and e['receipt']['manifest']['brand_sha256'] == version['brand_sha256']
               and e['receipt'].get('sharing_scope','admins_only') == sharing_scope]
    records = [e['receipt'] for e in entries]
    if found:
        write_atomic(state, json.dumps(found, ensure_ascii=False,sort_keys=True).encode())
        return {'status':'reused', 'receipt':found, 'legacy_files':legacy,
                'checked_at':datetime.now(timezone.utc).isoformat()}
    if records:
        previous = max(records,key=lambda r:r['accepted_at'])
        verify_registered(remote,next(e for e in entries if e['receipt'] == previous))
    suffix = '-'+sharing_scope if sharing_scope != 'admins_only' else ''
    token = remote.upload_private(html, 'geo-pending-'+version['version']+suffix+'.html')
    remote.verify_file(token)
    sha = hashlib.sha256(html).hexdigest()
    require(hashlib.sha256(remote.download(token)).hexdigest() == sha, '飞书完整性 / 上传后内容校验失败')
    return {'status':'pending_acceptance', 'file_token':token, 'url':remote.url(token), 'sharing_scope':sharing_scope,
            'manifest':version, 'html_sha256':sha, 'report':report(data), 'legacy_files':legacy,
            'previous': max(records,key=lambda r:r['accepted_at']) if records else None}


def accept(remote, candidate, proof, state):
    registry = read_registry(remote,state)
    require(candidate.get('status') == 'pending_acceptance', '飞书验收 / 没有待验收版本')
    receipt = {k:candidate[k] for k in ('manifest','file_token','url','html_sha256','report')}
    sharing_scope = candidate.get('sharing_scope','admins_only')
    receipt['sharing_scope'] = sharing_scope
    receipt.update(schema=SCHEMA, accepted=True, accepted_at=datetime.now(timezone.utc).isoformat(), proof=proof)
    valid_receipt(receipt)
    for entry in registry['records']:
        old = entry['receipt']
        if old['manifest'] == receipt['manifest'] and old.get('sharing_scope','admins_only') == sharing_scope:
            require(old['file_token'] == receipt['file_token'] and old['html_sha256'] == receipt['html_sha256'],
                    '飞书验收 / 已登记同版本的其他文件，不接纳重复候选')
            verify_registered(remote,entry)
            write_atomic(state,json.dumps(old,ensure_ascii=False,sort_keys=True).encode())
            return {'status':'published','receipt':old}
    require(receipt['url'] == remote.url(receipt['file_token']), '飞书验收 / 链接与文件标识不一致')
    remote.verify_file(receipt['file_token'],sharing_scope=sharing_scope)
    require(hashlib.sha256(remote.download(receipt['file_token'])).hexdigest() == receipt['html_sha256'],
            '飞书验收 / 验收期间文件变化，保留上次入口')
    body = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
    # A retry must reuse the timestamp and bytes already bound to the upload journal.
    prepared = state.with_name('accepting-'+digest([receipt['file_token'],sharing_scope])+'.json')
    if prepared.exists():
        try:
            saved_body = prepared.read_bytes(); saved = json.loads(saved_body)
        except (OSError, ValueError):
            raise ArchiveError('飞书验收 / 待提交回执损坏，请管理员核查') from None
        valid_receipt(saved)
        expected = json.loads(body); expected['accepted_at'] = saved['accepted_at']
        require(saved == expected, '飞书验收 / 待提交回执冲突，请管理员核查，不覆盖')
        body = saved_body
    else:
        write_atomic(prepared,body)
    receipt = json.loads(body)
    suffix = '-'+sharing_scope if sharing_scope != 'admins_only' else ''
    token = remote.upload_private(body, 'geo-accepted-'+candidate['manifest']['version']+suffix+'.json')
    remote.verify_file(token)
    require(remote.download(token) == body, '飞书验收 / 已验收记录回读失败，未切换入口')
    registry['records'].append({'receipt_token':token,'receipt':receipt})
    write_atomic(state.with_name('registry.json'),json.dumps(registry,ensure_ascii=False,sort_keys=True).encode())
    write_atomic(state, body)
    return {'status':'published', 'receipt':receipt}
