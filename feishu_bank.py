"""Read a Feishu question bank through the official CLI; never write to Feishu."""
import argparse
import csv
from datetime import datetime, timezone
import io
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile

from answerbit import MARKETS, load_bank
from archive import ArchiveError, require, write_atomic


def cli_read(args):
    cli = os.environ.get('LARK_CLI') or shutil.which('lark-cli')
    require(cli, '飞书题库 / 当前 Agent 尚未配置题库读取工具，请由 Agent 检查已有官方飞书工具；无需用户提供程序组件')
    try:
        result = subprocess.run([cli, *args, '--as', 'bot', '--format', 'json'],
                                capture_output=True, text=True, timeout=120)
        payload = json.loads(result.stdout if result.returncode == 0 else result.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ArchiveError('飞书题库 / CLI 未返回有效响应，请核对网络、钥匙串及应用配置；旧数据保留') from None
    if result.returncode != 0 or payload.get('ok') is not True:
        error = payload.get('error', {})
        scopes = ', '.join(error.get('missing_scopes', []))
        reason = ('当前 Agent 尚未获准读取飞书题库；请由 Agent 区分账号授权、应用权限与文件阅读权限，需要企业管理员审批时先报告并等待用户决定，不要求用户提供权限代码'
                  if scopes else '当前 Agent 暂时无法读取在线题库；请由 Agent 核对登录、文件阅读权限及网络，不把所有失败都当成缺权限')
        details = f'；管理员核对信息：{scopes}' if scopes else ''
        raise ArchiveError(f"飞书题库 / {reason}；错误码 {error.get('code', '未知')}{details}；停止后续取数，旧数据保留")
    return payload['data']


def signature(info):
    return (info.get('token'), info.get('revision'),
            [(s.get('sheet_id'), s.get('sheet_name'), s.get('row_count'), s.get('column_count'))
             for s in info.get('sheets', [])])


def content_signature(path):
    """Compare cell contents across browser exports and canonical API exports, ignoring ZIP formatting."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ArchiveError('题库读取 / 当前 Agent 缺少读取 Excel 的程序组件，请由 Agent 检查运行环境并按项目依赖处理，无需用户提供组件') from None
    try:
        book = load_workbook(io.BytesIO(path.read_bytes()),read_only=True,data_only=True,keep_links=False)
    except (BadZipFile,KeyError,ValueError):
        raise ArchiveError('飞书题库 / 导出或缓存不可解析，停止核对，旧快照保留') from None
    contents = {}
    try:
        for name in sorted(set(book.sheetnames) & set(MARKETS)):
            sheet = book[name]; sheet.reset_dimensions()
            rows = []
            for row in sheet.iter_rows(values_only=True):
                values = [None if v=='' else v for v in row]
                while values and values[-1] is None: values.pop()
                rows.append(values)
            while rows and not rows[-1]: rows.pop()
            contents[name] = rows
    finally:
        book.close()
    require(contents, '飞书题库 / 没有可核对的国家题单')
    return hashlib.sha256(json.dumps(contents,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def fetch_bank(url, destination, read=cli_read):
    """Validate every country sheet before atomically replacing the local bank."""
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise ArchiveError('题库读取 / 当前 Agent 缺少读取 Excel 的程序组件，请由 Agent 检查运行环境并按项目依赖处理，无需用户提供组件') from None
    before = read(['sheets', '+workbook-info', '--url', url])
    require(before.get('revision') is not None, '飞书题库 / 缺少修订号，无法校验读取期间变更')
    sheets = [s for s in before.get('sheets', []) if s.get('sheet_name') in MARKETS]
    require({s['sheet_name'] for s in sheets} == set(MARKETS) and len(sheets) == len(MARKETS),
            '飞书题库 / 国家工作表缺失或重名，请在飞书核对 11 个国家题单')
    book = Workbook(); book.remove(book.active)
    book.properties.created = book.properties.modified = datetime(2000, 1, 1)
    for sheet in sheets:
        name = sheet['sheet_name']; rows = sheet.get('row_count', 0); cols = sheet.get('column_count', 0)
        require(sheet.get('resource_type') == 'sheet' and 1 <= rows <= 100000 and 1 <= cols <= 1000,
                f'飞书题库 / {name} 网格结构异常')
        ws = book.create_sheet(name)
        for start in range(1, rows + 1, 100):
            end = min(start + 99, rows)
            value = read(['sheets', '+csv-get', '--spreadsheet-token', before['token'],
                          '--sheet-id', sheet['sheet_id'], '--range', f'A{start}:{get_column_letter(cols)}{end}',
                          '--include-row-prefix=false', '--max-chars', '2000000'])
            require(not value.get('has_more') and not value.get('truncated'),
                    f'飞书题库 / {name} 第 {start}–{end} 行被截断，请缩小读取窗口')
            require(value.get('revision') == before['revision'], f'飞书题库 / {name} 读取期间发生修改，请重试')
            require(value.get('row_indices') == list(range(start, end + 1)) and
                    value.get('col_count') == cols, f'飞书题库 / {name} 返回范围不完整')
            records = list(csv.reader(io.StringIO(value.get('annotated_csv', ''))))
            require(len(records) == end - start + 1 and all(len(r) == cols for r in records),
                    f'飞书题库 / {name} 返回行列数不符')
            for record in records:
                # Treat source cells as text; never turn question text into Excel formulas.
                for i, text in enumerate(record, 1):
                    text = text.strip() if start == 1 else text
                    if not text.strip(): text = ''
                    cell = ws.cell(start, i, text or None)
                    if text: cell.data_type = 's'
                start += 1
    after = read(['sheets', '+workbook-info', '--url', url])
    require(signature(before) == signature(after), '飞书题库 / 全量读取期间版本或结构变更，请重新运行')
    raw = io.BytesIO(); book.save(raw); book.close()
    # Stable ZIP timestamps keep the same content hash across unchanged refreshes.
    canonical = io.BytesIO()
    with ZipFile(raw) as source, ZipFile(canonical, 'w', ZIP_DEFLATED) as target:
        for name in source.namelist():
            from zipfile import ZipInfo
            entry = ZipInfo(name, (2000, 1, 1, 0, 0, 0)); entry.compress_type = ZIP_DEFLATED
            data = source.read(name)
            if name == 'docProps/core.xml':
                data = re.sub(rb'(<dcterms:(?:created|modified)[^>]*>)[^<]+', rb'\g<1>2000-01-01T00:00:00Z', data)
            target.writestr(entry, data)
    content = canonical.getvalue()
    with tempfile.TemporaryDirectory() as directory:
        check = Path(directory) / 'bank.xlsx'; check.write_bytes(content)
        version, bank, _ = load_bank(check)
    write_atomic(destination, content)
    return {'source': 'feishu-live', 'revision': before['revision'], 'title': before.get('title'),
            'read_at': datetime.now(timezone.utc).isoformat(), 'question_version': version,
            'question_count': len(bank), 'markets': len(sheets)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--url', default=os.environ.get('GEO_QUESTION_BANK_URL'))
    args = parser.parse_args()
    try:
        require(args.url, '飞书题库 / 请在本机配置 GEO_QUESTION_BANK_URL')
        print(json.dumps(fetch_bank(args.url, args.output), ensure_ascii=False))
    except (ArchiveError, OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
