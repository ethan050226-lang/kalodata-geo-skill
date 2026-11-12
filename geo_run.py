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


def run(bank, snapshot, output, options=()):
    config = {}; batch = None
    try:
        config = load_config()
        # The default shared run must not silently use an old exported bank.
        import os
        if not os.environ.get('GEO_QUESTION_BANK_URL'):
            raise ArchiveError('飞书题库 / 默认运行需配置最新在线题库；离线分析使用 dashboard.py，不自动发布')
        errors = []
        with redirect_stdout(io.StringIO()):
            result = dashboard.main(['refresh',str(bank),str(snapshot),str(output),*options],status_callback=errors.append)
        if result:
            why = '读取或校验失败；请查看本次具体失败环节，不发布新版本'
            saved = errors[-1] if errors else {}
            if saved:
                why = saved['stage']+' / '+saved['error']
            return failure(config,'取数与数据校验',why,saved.get('requested_window')), 1
        batch = dashboard.retained_data(output)[1]['batch']
        return publish(output,snapshot,config), 0
    except (ArchiveError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        why = str(error) if isinstance(error,ArchiveError) else '本地配置、文件或数据库异常，保留上一成功入口'
        return failure(config,'自动交付',why,batch), 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bank',type=Path); parser.add_argument('snapshot',type=Path); parser.add_argument('output',type=Path)
    parser.add_argument('--begin'); parser.add_argument('--end'); parser.add_argument('--excel',type=Path)
    args = parser.parse_args(); options = []
    for name in ('begin','end','excel'):
        if getattr(args,name) is not None: options.extend(['--'+name,str(getattr(args,name))])
    result, code = run(args.bank,args.snapshot,args.output,options)
    print(json.dumps(result,ensure_ascii=False)); return code


if __name__ == '__main__':
    raise SystemExit(main())
