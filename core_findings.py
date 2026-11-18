"""Bind Agent-written findings to exact, validated country/model slices; no model API or data fetch."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from archive import ArchiveError, require, write_atomic

VERSION = 'core-findings/1'
GENERIC_REMINDERS = ('样本构成可能不同','不代表整体表现','不能据此推断','仅供参考','建议进一步核实')


def fingerprint(data):
    source = {k:data.get(k) for k in ('batch','rows','analysis_version','question_scope','model_policy')}
    source['batch'] = {k:v for k,v in (source['batch'] or {}).items() if k!='fetched_at_utc'}
    return hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def metrics(rows):
    n = sum(r['n'] for r in rows)
    unnamed = [r for r in rows if not r['d'][4]]
    return {
        'mention': [sum(r['mentions'].get('Kalodata',0)-r.get('negative_mentions',0) for r in unnamed),
                    sum(r['n'] for r in unnamed)],
        'first': [sum(r['first'] for r in rows),n],
        'negative': [sum(r['sent'].get('negative',0) for r in rows),n],
        'mixed': [sum(r['sent'].get('mixed',0) for r in rows),n],
    }


def required_scopes(data):
    from market_scope import validate_data_scope
    validate_data_scope(data)
    require(data.get('model_policy') and data.get('market_scope'),
            '核心发现 / 该批未校验国家模型范围，请先从同批母库按已确认配置生成国家副本')
    require(data.get('question_scope',{}).get('answer_source_sha256'),
            '核心发现 / 缺少回答来源校验信息，请从同批数据库重新生成，不重取数')
    by_country = {}
    for index, row in enumerate(data['rows']):
        by_country.setdefault(row['d'][0], []).append(index)
    return [('overall', list(range(len(data['rows'])))),
            *[(market, by_country[market]) for market in sorted(by_country)]]


def context(data):
    scopes = required_scopes(data)
    return {'version':VERSION,'data_sha256':fingerprint(data),'batch':data['batch'],
            'models':data['model_policy']['models'],
            'markets':data['markets'],'groups':data['groups'],'tags':data['tags'],
            'required_scopes':[{'name':name,'row_indices':ids,'metrics':metrics([data['rows'][i] for i in ids])}
                               for name,ids in scopes],
            'rows':[{'index':i,'dimensions':r['d'],'valid':r['n'],'metrics':metrics([r]),
                     'references':list(r.get('evidence',{}).values())}
                    for i,r in enumerate(data['rows'])],
            'instructions':'运行Agent分析同批数据，为overall及每个国家写发现；可增加平台/题类切片。'
                           'entries每项含row_indices、conclusion、judgment、metrics。'
                           '每句写清国家/题类、指标、数值及下一步，均须有本批证据。'
                           'metrics使用同切片核对值；conclusion写事实，judgment写具体行动，证据不足标待核查。'
                           '不机械挑最低比例；不写泛化提醒。具体缺失放数据提醒，方法放指标说明，数字旁列分子分母。'}


def validate(data, findings=None):
    from feishu_delivery import reject_secrets
    value = data.get('core_findings') if findings is None else findings
    require(isinstance(value,dict) and set(value)=={'version','data_sha256','entries'}
            and value['version']==VERSION and value['data_sha256']==fingerprint(data),
            '核心发现 / 缺少同批Agent分析或数据已变化；保留上一成功版本，重新分析当前批次')
    required = required_scopes(data)
    reject_secrets(value)
    require(isinstance(value['entries'],list) and value['entries'],'核心发现 / 分析结果为空')
    scopes = set()
    for item in value['entries']:
        require(isinstance(item,dict) and set(item)=={'row_indices','conclusion','judgment','metrics'},
                '核心发现 / 分析结构不完整或含未审核字段')
        ids = item['row_indices']
        require(isinstance(ids,list) and ids and all(type(i) is int and 0<=i<len(data['rows']) for i in ids)
                and ids==sorted(set(ids)), '核心发现 / 范围必须是同批唯一有效行号')
        require(tuple(ids) not in scopes,'核心发现 / 同一范围不能重复写入结论')
        scopes.add(tuple(ids))
        require(all(isinstance(item[k],str) and len(item[k])<=400 for k in ('conclusion','judgment'))
                and item['conclusion'].strip(),'核心发现 / 结论不能为空或过长')
        require(not any(phrase in item[k] for phrase in GENERIC_REMINDERS for k in ('conclusion','judgment')),
                '核心发现 / 请删除泛化提醒；用本批数量、具体原因和下一步说明问题')
        require(item['metrics']==metrics([data['rows'][i] for i in ids]),
                '核心发现 / 指标、分子分母与所分析范围不一致')
    require(all(tuple(indices) in scopes for _, indices in required),
            '核心发现 / 必须分别分析总体及各国家，不能用一条全批结论代替')
    return value


def apply(source, findings_file):
    from dashboard import retained_data, render
    require(not source.with_suffix('.status.json').exists(),'核心发现 / 当前批次刷新失败，停止写入')
    data = retained_data(source)[1]
    findings = json.loads(findings_file.read_text())
    validate(data,findings)
    updated = deepcopy(data); updated['core_findings'] = findings
    from publication import report
    summary = report(updated)
    render(updated,source)
    return {'status':'analysis_ready','batch':data['batch'],'scopes':len(findings['entries']),
            'report':summary,
            'next_step':'仅本地交付时返回本看板及同批简报；需要飞书链接时用同批HTML和数据库继续发布'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('context','apply'))
    parser.add_argument('dashboard',type=Path);parser.add_argument('json_file',type=Path)
    args=parser.parse_args()
    try:
        if args.mode=='context':
            from dashboard import retained_data
            data=retained_data(args.dashboard)[1]
            require(args.dashboard.resolve()!=args.json_file.resolve(),'核心发现 / 上下文不能覆盖看板')
            write_atomic(args.json_file,json.dumps(context(data),ensure_ascii=False).encode())
            result={'status':'analysis_context_ready','next_step':'Agent读取上下文，独立分析各国家并写入JSON，再执行apply'}
        else:result=apply(args.dashboard,args.json_file)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (ArchiveError,OSError,ValueError,KeyError,TypeError) as error:
        why=str(error) if isinstance(error,ArchiveError) else '核心发现文件或数据结构异常；原看板保留'
        print(json.dumps({'status':'failed','reason':why},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
