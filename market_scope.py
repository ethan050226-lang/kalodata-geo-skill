"""Create a physically country-limited snapshot before rendering or sharing."""
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

from archive import require


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


def restrict_snapshot(source, target, markets):
    from recommendation_review import saved_review, write_review, identity, sidecar
    markets = normalize_markets(markets)
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
        for candidates, in db.execute("SELECT candidate_question_ids FROM issue WHERE coalesce(question_id,'')='' AND coalesce(candidate_question_ids,'')<>''"):
            require(not (set(candidates.split('、')) & questions),
                    '题号对账 / 所选国家存在尚未确认的候选编号，停止交付')
        with db:
            db.execute(f'DELETE FROM question WHERE market NOT IN ({placeholders})', markets)
            db.execute('DELETE FROM sample WHERE question_id NOT IN (SELECT question_id FROM question)')
            db.execute("DELETE FROM issue WHERE coalesce(question_id,'') NOT IN (SELECT question_id FROM question)")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('sample_group','sample_tag','sample_website','sample_content','sample_question','sample_answer'):
                if table in tables:
                    db.execute(f'DELETE FROM {table} WHERE task_id NOT IN (SELECT task_id FROM sample)')
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
            db.execute('CREATE TABLE delivery_scope (markets TEXT NOT NULL)')
            db.execute('INSERT INTO delivery_scope VALUES (?)', (json.dumps(markets),))
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
    require(set(data['markets']) <= markets and set(data['competitors']) <= markets
            and data.get('platform') is None, '国家范围 / 存在范围外国家或品牌全量平台快照')
    for row in data['rows']:
        require(row['d'][0] in markets, '国家范围 / 指标包含其他国家')
        for ref in row.get('evidence',{}).values():
            require(ref['d'][0] in markets, '国家范围 / 证据包含其他国家')
    references = list(data.get('neutral') or []) + list(data.get('recommendations') or [])
    references += (data.get('review') or {}).get('rows',[])
    require(all(ref['d'][0] in markets for ref in references), '国家范围 / 题单包含其他国家')
    comparison = data.get('comparison') or {}
    if comparison.get('previous_batch'):
        require(set(comparison.get('delivery_markets') or []) == markets
                and set(comparison.get('previous_delivery_markets') or []) == markets,
                '国家范围 / 上次对比缺少同国家范围证据，停止共享')
