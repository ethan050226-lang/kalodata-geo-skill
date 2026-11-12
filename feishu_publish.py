"""Publish versioned dashboards through the official Feishu CLI, after real preview checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit

from archive import ArchiveError, require, write_atomic
from dashboard import retained_data, render
from feishu_delivery import shared_data
from publication import manifest, stage, accept

PLACEHOLDER = b'GEO private upload reservation. No business data.'


class Drive:
    def __init__(self, config):
        self.cli = os.environ.get('LARK_CLI') or shutil.which('lark-cli')
        require(self.cli, '飞书配置 / 缺少官方 lark-cli')
        url = urlsplit(config['folder_url'])
        require(url.scheme == 'https' and url.hostname and url.hostname.endswith('.feishu.cn')
                and re.fullmatch(r'/drive/folder/[A-Za-z0-9]+', url.path), '飞书配置 / 目录必须为获批飞书文件夹')
        self.host, self.folder = url.hostname, url.path.rsplit('/',1)[-1]
        self.allowed = {(r['member_type'], r['member_id']) for r in config['approved_members']}
        require(self.allowed, '飞书配置 / 请配置已批准管理员及应用成员的精确身份，不用姓名作权限判定')
        self.identity = config.get('identity', 'bot')
        require(self.identity in ('bot','user'), '飞书配置 / 身份只允许 bot 或 user')

    def call(self, args, cwd=None):
        try:
            done = subprocess.run([self.cli, *args, '--as',self.identity,'--format','json'],
                                  cwd=cwd, capture_output=True, text=True, timeout=180)
            reply = json.loads(done.stdout if done.returncode == 0 else done.stderr)
        except (OSError,ValueError,subprocess.TimeoutExpired):
            raise ArchiveError('飞书调用 / 超时或响应不可解析，停止发布；请核对应用权限、网络和钥匙串') from None
        require(isinstance(reply,dict), '飞书调用 / 响应不是有效对象，停止发布')
        require(done.returncode == 0 and reply.get('ok') is True,
                f"飞书调用 {' '.join(args[:2])} / 错误码 {reply.get('error',{}).get('code','未知')}，请在飞书权限管理核对；未更新成功入口")
        require(isinstance(reply.get('data'),dict), '飞书调用 / 成功响应缺少有效 data 对象')
        return reply['data']

    def list_files(self):
        files, seen, token = [], set(), None
        while True:
            params = {'folder_token':self.folder,'page_size':200}
            if token: params['page_token'] = token
            data = self.call(['drive','files','list','--params',json.dumps(params)])
            require(isinstance(data.get('files'),list) and type(data.get('has_more')) is bool,
                    '飞书查重 / 文件清单缺少分页或文件字段')
            require(all(isinstance(f,dict) and isinstance(f.get('name'),str)
                        and re.fullmatch(r'[A-Za-z0-9]+',str(f.get('token',''))) for f in data['files']),
                    '飞书查重 / 文件名称或标识无效')
            files.extend(data['files'])
            if not data['has_more']: return files
            token = data.get('next_page_token')
            require(token and token not in seen, '飞书查重 / 分页未取全或游标重复')
            seen.add(token)

    def verify_file(self, token, kind='file'):
        require(isinstance(token,str) and re.fullmatch(r'[A-Za-z0-9]+',token), '飞书权限 / 文件标识无效')
        public = self.call(['drive','+permission-get-setting','--token',token,'--type',kind]).get('permission_public',{})
        require(public.get('link_share_entity') == 'closed' and public.get('external_access') is False,
                '飞书权限 / 链接分享未关闭或外部访问未禁用；到文件分享设置收紧后重试，未发布业务数据')
        # Check both direct and inherited folder grants; never silently allow a group/team.
        for resource, resource_type in ((self.folder,'folder'),(token,kind)):
            members = self.call(['drive','+member-list','--token',resource,'--type',resource_type])
            require(isinstance(members.get('items'),list) and not members.get('has_more'),
                    '飞书权限 / 成员清单不完整')
            require(all((m.get('member_type'),m.get('member_id')) in self.allowed for m in members['items']),
                    '飞书权限 / 存在未批准的直接或继承协作者，请到文件及父文件夹核对')

    def download(self, token):
        with tempfile.TemporaryDirectory() as directory:
            data = self.call(['drive','+preview','--file-token',token,'--type','source_file','--output','source'],cwd=directory)
            path = Path(data.get('output_path',''))
            path = path if path.is_absolute() else Path(directory)/path
            require(path.resolve().is_relative_to(Path(directory).resolve()) and path.is_file(),
                    '飞书完整性 / 下载文件路径无效')
            return path.read_bytes()

    def upload(self, body, name, token=None):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'upload').write_bytes(body)
            args = ['drive','+upload','--file','upload','--name',name,'--folder-token',self.folder]
            if token: args.extend(['--file-token',token])
            data = self.call(args,cwd=directory)
            result = data.get('file_token')
            require(isinstance(result,str) and result and (not token or result == token),
                    '飞书上传 / 缺少文件标识或原地更新未保留文件标识；停止切换入口')
            return result

    def upload_private(self, body, name):
        files = [r for r in self.list_files() if r['name'] == name]
        require(len(files) <= 1, '飞书上传 / 发现同名候选冲突，请管理员核查，不自动覆盖')
        token = files[0]['token'] if files else self.upload(PLACEHOLDER,name)
        # Upload harmless bytes first: tenant defaults may expose newly created files.
        self.verify_file(token)
        current = self.download(token)
        require(current in (PLACEHOLDER,body), '飞书上传 / 已有文件内容冲突，不覆盖历史版本')
        if current != body: self.upload(body,name,token)
        return token

    def url(self, token):
        require(re.fullmatch(r'[A-Za-z0-9]+',token), '飞书回查 / 文件标识无效')
        return f'https://{self.host}/file/{token}'


def load_config():
    path = os.environ.get('GEO_FEISHU_CONFIG')
    require(path and Path(path).is_file(), '飞书配置 / 管理员须先在获批环境设置 GEO_FEISHU_CONFIG；不在聊天提交凭证')
    config = json.loads(Path(path).read_text())
    require(isinstance(config,dict) and isinstance(config.get('state_dir'),str) and config['state_dir'],
            '飞书配置 / 缺少私有发布状态目录')
    return config


def publish(source, snapshot, config, proof=None, remote=None):
    state_dir = Path(config['state_dir']); state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    state = state_dir/'last-success.json'
    candidate_path = state_dir/'candidate.json'
    remote = remote or Drive(config)
    if proof is not None:
        candidate = json.loads(candidate_path.read_text())
        result = accept(remote,candidate,proof,state)
    else:
        require(not source.with_suffix('.status.json').exists(), '发布校验 / 最新取数失败，停止发布')
        _, data, _ = retained_data(source)
        require(not any(v != '响应无可用数据' for v in (data.get('platform') or {}).get('issues',{}).values()),
                '发布校验 / 平台快照读取失败，停止发布；请回 Answerbit 核查')
        version = manifest(snapshot,data)
        shared = shared_data(data)
        shared['delivery']['measurement_version'] = version['version']
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory)/'share.html'; render(shared,html)
            result = stage(remote,data,version,html.read_bytes(),state)
        if result['status'] == 'pending_acceptance':
            write_atomic(candidate_path,json.dumps(result,ensure_ascii=False).encode())
    return result


def failure(config, stage_name, message, batch=None):
    previous = None
    if config.get('state_dir'):
        path = Path(config['state_dir'])/'last-success.json'
        if path.is_file():
            try: previous = json.loads(path.read_text())
            except (ValueError,OSError): pass
    if previous is None: previous = config.get('previous_success')
    return {'status':'failed','stage':stage_name,'reason':message,'affected_batch':batch,
            'affected':'本次整批交付；上一成功批次不被替换', 'previous':previous,
            'previous_access':'仅为上次验收记录，本次访问状态尚未重新核实' if previous else '没有已记录的成功看板入口',
            'verify_at':'取数及口碑/引用回 Answerbit；题号回飞书题库；发布、访问和权限回飞书文件及应用权限管理'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path); parser.add_argument('snapshot',type=Path)
    parser.add_argument('--accept-proof',type=Path,help='Agent 完成实际浏览器验收后保存的私有证据；不是用户确认替代品')
    args = parser.parse_args(); config = {}
    try:
        config = load_config()
        proof = json.loads(args.accept_proof.read_text()) if args.accept_proof else None
        result = publish(args.source,args.snapshot,config,proof)
        print(json.dumps(result,ensure_ascii=False)); return 0
    except (ArchiveError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        why = str(error) if isinstance(error,ArchiveError) else '文件、配置或响应结构异常；保留上一入口'
        batch = None
        try: batch = retained_data(args.source)[1]['batch']
        except (OSError,ValueError,KeyError,IndexError): pass
        print(json.dumps(failure(config,'飞书查重、上传或验收',why,batch),ensure_ascii=False)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
