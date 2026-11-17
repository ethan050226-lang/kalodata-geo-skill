"""Create a physically country-limited snapshot before rendering or sharing."""
from contextlib import closing
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from archive import require

MODEL_POLICY_VERSION = 'country-model/1'
APPROVED_MODEL_POLICY = Path(__file__).parent/'skill/geo-sampling/references/country-models.json'
MODEL_ALIASES = {
    'chatgpt':'chatgpt','gpt':'chatgpt','gemini':'gemini','perplexity':'perplexity',
    'google ai mode':'google_ai_mode','ai mode':'google_ai_mode',
    'deepseek':'deepseek','豆包':'doubao','kimi':'kimi','千问':'qianwen',
    '通义千问':'qianwen','文心一言':'yiyan','腾讯元宝':'yuanbao',
}


def model_identifier(value):
    if not isinstance(value,str): return None
    value=' '.join(value.strip().casefold().split())
    return MODEL_ALIASES.get(value) or (value if re.fullmatch(r'[a-z][a-z0-9_-]*',value) else None)


def load_model_policy(bank, approved=None):
    """Reconcile explicit bank settings with sourced user decisions; never infer a whitelist."""
    from openpyxl import load_workbook
    from answerbit import MARKETS
    result={'version':MODEL_POLICY_VERSION,'bank_sha256':hashlib.sha256(bank.read_bytes()).hexdigest(),
            'models':{},'evidence':{}}
    book=load_workbook(bank,read_only=True,data_only=True,keep_links=False)
    try:
        for sheet in book:
            if sheet.title not in MARKETS and sheet.title!='国家模型配置': continue
            sheet.reset_dimensions(); rows=sheet.iter_rows(values_only=True);header=next(rows,())
            if sheet.title=='国家模型配置':
                require(header.count('国家')==1 and header.count('平台标识')==1,
                        '国家模型配置 / 配置页需有唯一“国家”和“平台标识”表头，不能猜列')
                country_col,model_col=header.index('国家'),header.index('平台标识')
            elif header.count('采样模型')==1:
                country_col=None;model_col=header.index('采样模型')
            else: continue
            per_country={}
            for line,row in enumerate(rows,2):
                if not any(v is not None for v in row): continue
                market=sheet.title if country_col is None else (row[country_col] if country_col<len(row) else None)
                market=MARKETS.get(market,market)
                if market=='UK': market='GB'
                value=row[model_col] if model_col<len(row) else None
                if value is None and country_col is None: continue
                require(market in MARKETS.values() and isinstance(value,str) and value.strip(),
                        f'国家模型配置 / {sheet.title} 第{line}行国家或模型为空，停止正式统计')
                models=[model_identifier(v) for v in re.split(r'[,，、;；\n]+',value)]
                require(models and all(models),f'国家模型配置 / {sheet.title} 第{line}行模型不能识别，需核对平台标识')
                if country_col is None and market in per_country:
                    require(per_country[market]==set(models),f'国家模型配置 / {sheet.title} 同国家行配置不一致')
                per_country[market]=set(models) if country_col is None else per_country.get(market,set())|set(models)
                result['evidence'].setdefault(market,[]).append(f'{sheet.title}!{line}')
            for market,models in per_country.items():
                require(market not in result['models'] or set(result['models'][market])==models,
                        '国家模型配置 / 不同配置位置存在冲突，停止正式统计')
                result['models'][market]=sorted(models)
    finally:
        book.close()
    if approved is not None:
        confirmed=json.loads(Path(approved).read_text())
        require(confirmed.get('version')==MODEL_POLICY_VERSION and
                isinstance(confirmed.get('countries'),dict) and isinstance(confirmed.get('platforms'),dict)
                and isinstance(confirmed.get('sources'),dict),
                '国家模型配置 / 已确认配置格式不完整')
        mapped=[item['id'] for item in confirmed['platforms'].values() if item.get('id') is not None]
        require(len(mapped)==len(set(mapped)),
                '国家模型配置 / 不同模型不能合并为同一平台标识，尤其 AIO、AI Mode 与 Gemini')
        pending={}
        deferred=confirmed.get('deferred_models',{})
        require(isinstance(deferred,dict) and all(name in confirmed['platforms'] and source in confirmed['sources']
                for name,source in deferred.items()),'国家模型配置 / 暂缓模型必须保留确认来源')
        if deferred: result['deferred_models']=deferred
        if confirmed['countries']: result['requested_models']={}
        for market,entry in confirmed['countries'].items():
            require(market in MARKETS.values() and entry.get('source') in confirmed['sources']
                    and entry.get('status') in ('complete','incomplete') and entry.get('models'),
                    '国家模型配置 / 国家、来源或完整状态未确认')
            active=[name for name in entry['models'] if name not in deferred]
            require(active,'国家模型配置 / 本轮没有启用模型，不生成正式统计')
            models=[];unknown=[]
            for name in active:
                item=confirmed['platforms'].get(name,{})
                platform=item.get('id')
                if platform is None: unknown.append(name);continue
                require(model_identifier(platform)==platform and item.get('evidence'),
                        '国家模型配置 / 平台标识须有来源证据，不猜标识')
                models.append(platform)
            if unknown or entry['status']!='complete':
                pending[market]=([entry.get('pending_reason') or '完整模型名单待确认'] if entry['status']!='complete' else [])+[
                    name+' 的 Answerbit 平台标识待确认' for name in unknown]
            else:
                require(market not in result['models'] or set(result['models'][market])==set(models),
                        '国家模型配置 / 题库与用户已确认配置冲突，需核对，不静默覆盖')
            result['models'][market]=sorted(set(models))
            result['requested_models'][market]={name:confirmed['platforms'][name].get('id') for name in active}
            result['evidence'][market]=result['evidence'].get(market,[])+[
                '已确认配置:'+entry['source']]+[
                name+':'+confirmed['platforms'][name]['evidence'] for name in active
                if confirmed['platforms'].get(name,{}).get('id') is not None]+[
                '本轮暂缓:'+name+':'+deferred[name] for name in entry['models'] if name in deferred]
        if pending: result['pending']=pending
    require(not (set(result['models'].get('CN',[])) & {'gemini','perplexity','google_ai_mode'}),
            '国家模型配置 / 中国配置与已确认排除 Gemini、Perplexity、AI Mode 的要求冲突，需核对')
    result['signature']=hashlib.sha256(json.dumps(result,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return result


def sampling_plan(policy, markets):
    """A reviewable requirement list, never a request to create paid platform tasks."""
    markets=normalize_markets(markets)
    return {'state':'plan_only','creates_tasks':False,'platform_sampling_verified':False,
            'countries':{m:policy.get('requested_models',{}).get(m,{p:p for p in policy['models'].get(m,[])}) for m in markets},
            'deferred_models':policy.get('deferred_models',{}),
            'pending':{m:policy['pending'][m] for m in markets if m in policy.get('pending',{})},
            'next_step':'核对 Answerbit 各国提问的启用模型、频次、地区与执行结果；本工具仅读取已有回答，不创建采样任务'}


def require_model_policy(policy, markets):
    markets=normalize_markets(markets)
    pending={m:policy.get('pending',{}).get(m) for m in markets if policy.get('pending',{}).get(m)}
    require(not pending,'国家模型配置 / '+ '；'.join(m+'：'+'、'.join(reasons) for m,reasons in pending.items())+
            '；停止对应正式统计，保留上一批')
    missing=[m for m in markets if not policy.get('models',{}).get(m)]
    require(not missing,'国家模型配置 / '+ '、'.join(missing)+' 未在题库找到明确配置；停止正式统计，不猜白名单')


def normalize_markets(values):
    from answerbit import MARKETS
    require(isinstance(values,(list,tuple)) and all(isinstance(v,str) and v.strip() for v in values),
            '运营国家 / 请提供国家名称或代码列表')
    result = sorted({MARKETS.get(value, value.upper()) for value in values})
    require(result and set(result) <= set(MARKETS.values()),
            '运营国家 / 请确认一个或多个有效国家名称或代码，不默认发送全量')
    return result


def scope_config(config, markets):
    """Keep accepted publication state separate for each country's subscription."""
    key = 'markets-'+'-'.join(normalize_markets(markets))
    result = dict(config)
    if result.get('state_dir'):
        root = Path(result['state_dir'])
        result['state_dir'] = str(root if root.name == key else root/key)
    result.pop('previous_success',None)  # A legacy unscoped receipt is not this country's baseline.
    return result


def restrict_snapshot(source, target, markets, *, model_policy=None):
    from recommendation_review import saved_review, write_review, identity, sidecar
    markets = normalize_markets(markets)
    if model_policy is not None: require_model_policy(model_policy,markets)
    require(source.resolve() != target.resolve(), '国家范围 / 裁剪不得覆盖源快照')
    review = saved_review(source)
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as origin, \
            closing(sqlite3.connect(target)) as db:
        origin.backup(db)
        db.execute('PRAGMA secure_delete=ON')
        placeholders = ','.join('?' for _ in markets)
        questions = {r[0] for r in db.execute(
            f'SELECT question_id FROM question WHERE market IN ({placeholders})', markets)}
        require(questions, '运营国家 / 题库没有所选国家，保留上一批')
        if model_policy is not None:
            require(db.execute('SELECT question_version FROM batch').fetchone()[0]==model_policy['bank_sha256'],
                    '国家模型配置 / 配置与母批题库版本不同，不混用')
        for candidates, in db.execute("SELECT candidate_question_ids FROM issue WHERE coalesce(question_id,'')='' AND coalesce(candidate_question_ids,'')<>''"):
            require(not (set(candidates.split('、')) & questions),
                    '题号对账 / 所选国家存在尚未确认的候选编号，停止交付')
        with db:
            db.execute(f'DELETE FROM question WHERE market NOT IN ({placeholders})', markets)
            db.execute('DELETE FROM sample WHERE question_id NOT IN (SELECT question_id FROM question)')
            audit=[]
            if model_policy is not None:
                db.execute('DROP TABLE IF EXISTS country_model_audit')
                db.execute('CREATE TABLE country_model_audit (task_id TEXT,question_id TEXT,market TEXT,platform TEXT,task_date TEXT,reason TEXT,prompt TEXT)')
                known=set(MODEL_ALIASES.values())|{m for models in model_policy['models'].values() for m in models}
                for task,number,market,platform,day,prompt in db.execute('SELECT s.task_id,s.question_id,q.market,s.platform,s.task_date,p.prompt FROM sample s JOIN question q USING(question_id) JOIN sample_question p USING(task_id)'):
                    canonical=model_identifier(platform)
                    if canonical not in model_policy['models'][market]:
                        audit.append((task,number,market,platform,day,'模型未识别' if canonical not in known else '不在该国家已确认模型范围',prompt))
                db.executemany('INSERT INTO country_model_audit VALUES (?,?,?,?,?,?,?)',audit)
                db.executemany('DELETE FROM sample WHERE task_id=?',[(r[0],) for r in audit])
                db.executemany('DELETE FROM issue WHERE task_id=?',[(r[0],) for r in audit])
            db.execute("DELETE FROM issue WHERE coalesce(question_id,'') NOT IN (SELECT question_id FROM question)")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('sample_group','sample_tag','sample_website','sample_content','sample_question','sample_answer','sample_quality','sample_platform'):
                if table in tables:
                    db.execute(f'DELETE FROM {table} WHERE task_id NOT IN (SELECT task_id FROM sample)')
            if 'sample_quality' in tables:
                db.execute('UPDATE sample_quality SET reference_task=NULL,reference_units=NULL,ratio=NULL '
                           'WHERE reference_task NOT IN (SELECT task_id FROM sample)')
            require(db.execute("SELECT count(*) FROM sample WHERE status='valid'").fetchone()[0] > 0,
                    '运营国家 / 所选国家没有有效回答，保留上一批')
            count = db.execute('SELECT count(*) FROM (SELECT task_id FROM sample UNION SELECT task_id FROM issue)').fetchone()[0]
            db.execute('UPDATE batch SET returned_records=?', (count,))
            # The source hash remains the complete retrieval fingerprint; no raw response leaves the local snapshot.
            scope = sorted(set(db.execute('SELECT s.question_id,s.platform,s.language,coalesce(g.group_id,\'\'),coalesce(t.tag_id,\'\') '
                     'FROM sample s LEFT JOIN sample_group g USING(task_id) LEFT JOIN sample_tag t USING(task_id) '
                     'ORDER BY s.question_id,s.platform,s.language,g.group_id,t.tag_id')))
            db.execute('UPDATE retrieval_scope SET signature=?',
                       (hashlib.sha256(json.dumps(scope,ensure_ascii=False).encode()).hexdigest(),))
            db.execute('DROP TABLE IF EXISTS delivery_scope')
            db.execute('CREATE TABLE delivery_scope (markets TEXT NOT NULL, model_policy TEXT)')
            scoped_policy=None
            if model_policy is not None:
                scoped_policy={**{k:v for k,v in model_policy.items() if k!='pending'},'models':{m:model_policy['models'][m] for m in markets},
                               'evidence':{m:model_policy['evidence'][m] for m in markets}}
                if 'requested_models' in model_policy:
                    scoped_policy['requested_models']={m:model_policy['requested_models'][m] for m in markets}
            db.execute('INSERT INTO delivery_scope VALUES (?,?)', (json.dumps(markets),json.dumps(scoped_policy) if scoped_policy else None))
        # Remove deleted originals from free pages, not only from SQL query results.
        db.execute('VACUUM')
        tasks = {r[0] for r in db.execute('SELECT task_id FROM sample')}
    if review:
        write_review(target, identity(target), {k:v for k,v in review['records'].items() if k in tasks})
    else:
        sidecar(target).unlink(missing_ok=True)
    return markets


def validate_data_scope(data):
    """Fail closed on country leakage at the render/publication boundary."""
    scope = data.get('market_scope')
    if not scope:
        return  # Legacy local, unscoped snapshots remain readable.
    markets = set(normalize_markets(scope['markets']))
    policy=data.get('model_policy')
    if policy:
        require(set(policy['models'])==markets and set(policy['evidence'])==markets
                and policy['bank_sha256']==data['batch']['question_version'],
                '国家模型范围 / 配置与国家或题库版本不一致')
        require(all(r['market'] in markets for r in policy['excluded_records']),
                '国家模型范围 / 审计包含其他国家')
        if 'requested_models' in policy:
            require(set(policy['requested_models'])==markets and all(
                set(models.values())==set(policy['models'][m]) and None not in models.values()
                for m,models in policy['requested_models'].items()),
                '国家模型范围 / 采样要求与已确认模型不一致或存在未知映射')
    require(set(data['markets']) <= markets and set(data['competitors']) <= markets
            and data.get('platform') is None, '国家范围 / 存在范围外国家或品牌全量平台快照')
    for row in data['rows']:
        require(row['d'][0] in markets, '国家范围 / 指标包含其他国家')
        if policy:
            require(model_identifier(row['d'][1]) in policy['models'].get(row['d'][0],[]),
                    '国家模型范围 / 指标包含题库范围外模型')
        for ref in row.get('evidence',{}).values():
            require(ref['d'][0] in markets, '国家范围 / 证据包含其他国家')
            if policy:
                require(model_identifier(ref['d'][1]) in policy['models'].get(ref['d'][0],[]),
                        '国家模型范围 / 证据包含范围外模型')
    references = list(data.get('neutral') or []) + list(data.get('recommendations') or [])
    references += (data.get('review') or {}).get('rows',[])
    references += (data.get('quality') or {}).get('records',[])
    require(all(ref['d'][0] in markets for ref in references), '国家范围 / 题单包含其他国家')
    if policy:
        require(all(model_identifier(ref['d'][1]) in policy['models'].get(ref['d'][0],[]) for ref in references),
                '国家模型范围 / 题单包含范围外模型')
    comparison = data.get('comparison') or {}
    if comparison.get('previous_batch'):
        require(set(comparison.get('delivery_markets') or []) == markets
                and set(comparison.get('previous_delivery_markets') or []) == markets,
                '国家范围 / 上次对比缺少同国家范围证据，停止共享')
