"""Prepare a shared dashboard with no answer originals or local database access."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

from archive import ArchiveError, require
from dashboard import retained_data, render

# Only question text and locating fields may leave the local analysis environment.
REFERENCE = {'q','text','category','task','d','status','label','reason','table'}
TOP = {'batch','platform','rows','issues','core_mention','analysis_version','prompt_basis',
       'question_named','question_scope','markets','competitors','tags','groups','neutral',
       'links','review','recommendations','comparison','bank_source','market_scope'}
FORBIDDEN = {'body','answer','answer_text','raw_answer','raw_response','access_token','app_secret',
             'api_key','apikey','authorization','password','originals'}


def reject_secrets(value):
    if isinstance(value, dict):
        require(not (set(k.lower() for k in value) & FORBIDDEN), '共享校验 / 数据包含原文或凭证字段，停止交付')
        for item in value.values(): reject_secrets(item)
    elif isinstance(value, list):
        for item in value: reject_secrets(item)
    elif isinstance(value, str):
        require(not re.search(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}',value), '共享校验 / 检出疑似凭证，停止交付')
        require(not re.search(r'(?:https?://)?(?:127\.0\.0\.1|localhost)(?::\d+)?',value,re.I),
                '共享校验 / 检出本机地址，停止交付')


def shared_data(data):
    from market_scope import validate_data_scope
    validate_data_scope(data)
    require(set(data) <= TOP, '共享校验 / 出现未审核的新数据字段，请先核查共享范围')
    reject_secrets(data)
    require(data.get('rows') and data.get('batch') and not data.get('last_failure'),
            '共享校验 / 批次不完整、为空或标记失败，不发布')
    result = deepcopy(data)
    for row in result['rows']:
        for signal, reference in row.get('evidence', {}).items():
            require(set(reference)<=REFERENCE,'共享校验 / 证据记录包含未审核字段')
    for name in ('neutral','recommendations'):
        if result.get(name) is not None:
            require(all(set(row)<=REFERENCE for row in result[name]),'共享校验 / 回查记录包含未审核字段')
    if result.get('review'):
        require(all(set(row)<=REFERENCE for row in result['review']['rows']), '共享校验 / 推荐记录包含未审核字段')
    require(sum(r['n'] for r in result['rows'])>0,'共享校验 / 无有效回答')
    result['delivery']={'mode':'feishu','originals':'answerbit-only',
                        'preview_validation':'pending','operator_access_validation':'pending'}
    return result


def prepare(source, output):
    require(source.resolve()!=output.resolve(),'共享校验 / 共享文件不得覆盖本地原版')
    require(not source.with_suffix('.status.json').exists(),'共享校验 / 最近刷新失败，请先核对状态文件；保留上次共享结果')
    _,data,_=retained_data(source)
    shared=shared_data(data)
    render(shared,output)
    return {'html':str(output),'batch':shared['batch'],'valid':sum(r['n'] for r in shared['rows']),
            'preview_validation':'pending','operator_access_validation':'pending'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    try: print(json.dumps(prepare(args.source,args.output),ensure_ascii=False))
    except (ArchiveError,OSError,ValueError,KeyError):
        parser.exit(1,'共享准备失败：请核对批次、数据共享字段和本地刷新状态；没有上传或覆盖飞书文件。\n')

if __name__=='__main__':main()
