"""Default Skill entry: refresh validated data, then deduplicate and stage Feishu delivery."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3

from archive import ArchiveError
import dashboard
from feishu_publish import load_config, publish, failure
from market_scope import normalize_markets, scope_config
from publication import report


def preflight(bank, markets):
    """Local prerequisite checks only. Never echo identifiers, URLs or credential values."""
    import answerbit
    markets = normalize_markets(markets)
    key,_ = answerbit.operator_credentials()  # Do not open private publishing configuration before the Key gate.
    problems = []; publication_warnings = []
    if not answerbit.question_bank_url(key):
        problems.append('缺少在线题库私有配置；真实取数前必须核对最新版，本地缓存不能替代')
    try:
        config = load_config()
        missing = [k for k in ('folder_url','approved_members') if not config.get(k)]
        if missing: publication_warnings.append('飞书发布配置待补：'+'、'.join(missing))
    except (ArchiveError,OSError,ValueError):
        publication_warnings.append('飞书发布未配置或不可读；本地分析可独立运行')
    return {'status':'preflight_only','markets':markets,'blockers':problems,
            'publication_warnings':publication_warnings,
            'next_step':'补齐取数前置条件后再刷新' if problems else '继续完整刷新；发布缺项不阻止已校验的本地看板交付',
            'verified':'仅核对本机配置；未请求Answerbit或飞书，平台权限和实时访问尚未验证'}


def run(bank, snapshot, output, options=(), *, markets=(), local_only=False):
    config = {}; batch = None
    try:
        markets = normalize_markets(markets)
        options = list(options)
        for market in markets:
            options.extend(['--market',market])
        errors = []
        with redirect_stdout(io.StringIO()):
            result = dashboard.main(['refresh',str(bank),str(snapshot),str(output),*options],status_callback=errors.append)
        if result:
            why = '读取或校验失败；请查看本次具体失败环节，不发布新版本'
            saved = errors[-1] if errors else {}
            if saved:
                why = saved['stage']+' / '+saved['error']
            if saved.get('stage') not in (None,'配置','只读凭证'):
                try: config = scope_config(load_config(), markets)
                except (ArchiveError,OSError,ValueError): pass
            return failure(config,'取数与数据校验',why,saved.get('requested_window')), 1
        data = dashboard.retained_data(output)[1]
        online = (data.get('bank_source') or {}).get('source') == 'feishu-live'
        batch = data['batch']
        local = {'dashboard':str(output.resolve()), 'snapshot':str(snapshot.resolve()), 'report':report(data)}
        blockers = []
        if not online:
            blockers.append('本次使用本地题库，未核实在线最新版；管理员配置 GEO_QUESTION_BANK_URL 后再发布')
        if local_only:
            return {'status':'local_ready','local':local,'publication':'用户选择仅本地，不上传飞书'}, 0
        try:
            config = scope_config(load_config(), markets)
            missing = [name for name in ('folder_url','approved_members') if not config.get(name)]
            if missing:
                blockers.append('飞书发布配置待补：'+ '、'.join(missing)+'；仅在获批私有环境配置，不发送实际值到聊天')
        except (ArchiveError,OSError,ValueError) as error:
            blockers.append(str(error) if isinstance(error,ArchiveError) else '飞书发布配置无法读取或解析')
        if blockers:
            return {'status':'local_ready_pending_publication', 'local':local, 'blockers':blockers,
                    'publication':'未上传，未替换上一成功飞书版本；请管理员配置独立发布授权，勿在聊天发送凭证'}, 0
        try:
            return publish(output,snapshot,config), 0
        except (ArchiveError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
            why = str(error) if isinstance(error,ArchiveError) else '飞书发布配置、文件或响应异常'
            return {'status':'local_ready_pending_publication','local':local,
                    'publication':failure(config,'飞书查重、上传或验收',why,batch)}, 0
    except (ArchiveError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        why = str(error) if isinstance(error,ArchiveError) else '本地配置、文件或数据库异常，保留上一成功入口'
        return failure(config,'自动交付',why,batch), 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bank',type=Path); parser.add_argument('snapshot',type=Path); parser.add_argument('output',type=Path)
    parser.add_argument('--begin'); parser.add_argument('--end'); parser.add_argument('--excel',type=Path)
    parser.add_argument('--market',action='append',required=True,help='用户确认的运营国家，可重复')
    parser.add_argument('--local-only',action='store_true',help='仅生成本地看板与简报，不调用飞书发布')
    parser.add_argument('--check',action='store_true',help='仅检查本机前置条件，不请求平台、不发布')
    args = parser.parse_args(); options = []
    for name in ('begin','end','excel'):
        if getattr(args,name) is not None: options.extend(['--'+name,str(getattr(args,name))])
    if args.check:
        try: result,code = preflight(args.bank,args.market),0
        except ArchiveError as error: result,code = {'status':'failed','stage':'运行前检查','reason':str(error)},1
    else:
        result, code = run(args.bank,args.snapshot,args.output,options,markets=args.market,local_only=args.local_only)
    print(json.dumps(result,ensure_ascii=False)); return code


if __name__ == '__main__':
    raise SystemExit(main())
