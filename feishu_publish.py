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
from dashboard import ANALYSIS_VERSION, retained_data, render
from feishu_delivery import shared_data
from publication import manifest, stage, accept

PLACEHOLDER = b'GEO private upload reservation. No business data.'


class PublicationAccessError(ArchiveError):
    def __init__(self, checks):
        super().__init__('飞书发布连接未通过；请一次核对完整权限清单，修复后仅重试发布，不重新取数')
        self.publication_check = checks


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
        self.identity = config.get('identity') or 'bot'
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

    def check_access(self):
        """Read-only probes; a configured upload scope is not proof of a successful write."""
        checks = []
        probes = (
            ('目录查重', 'space:document:retrieve', self.list_files),
            ('协作者读取', 'docs:permission.member:retrieve',
             lambda: self.call(['drive','+member-list','--token',self.folder,'--type','folder'])),
            ('分享设置读取', 'docs:permission.setting:read',
             lambda: self.call(['drive','+permission-get-setting','--token',self.folder,'--type','folder'])),
        )
        for name, scope, probe in probes:
            try:
                data = probe()
                if name == '协作者读取':
                    require(isinstance(data.get('items'),list) and not data.get('has_more'),
                            '飞书权限 / 协作者清单不完整，不能确认读取能力')
                elif name == '分享设置读取':
                    require(isinstance(data.get('permission_public'),dict) and data['permission_public'],
                            '飞书权限 / 缺少分享设置，不能确认读取能力')
                item = {'capability':name,'scope':scope,'status':'passed'}
            except ArchiveError as error:
                item = {'capability':name,'scope':scope,'status':'blocked','reason':str(error)}
            checks.append(item)
        for name, scope in (
            ('文件上传','drive:file:upload'), ('原文件回读','drive:file:download'),
            ('分享设置修改','docs:permission.setting:write_only'),
            ('新文件管理员授权（按需）','docs:permission.member:create'),
        ):
            checks.append({'capability':name,'scope':scope,'status':'not_tested'})
        return {'identity':self.identity,'checks':checks,
                'read_access_ready':all(c['status']=='passed' for c in checks[:3]),
                'write_access_verified':False,
                'guide':'skill/geo-sampling/references/feishu-preview.md#一次列清发布权限',
                'next_step':'结合当前应用权限清单核对；未验证不等于缺失。企业审核项先报告，不自动申请或修改权限。'}

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

    def verify_file(self, token, kind='file', sharing_scope='admins_only'):
        require(isinstance(token,str) and re.fullmatch(r'[A-Za-z0-9]+',token), '飞书权限 / 文件标识无效')
        require(sharing_scope in ('admins_only','tenant_readable'), '飞书权限 / 不支持的分享范围')
        public = self.call(['drive','+permission-get-setting','--token',token,'--type',kind]).get('permission_public',{})
        expected = 'tenant_readable' if sharing_scope == 'tenant_readable' else 'closed'
        require(public.get('link_share_entity') == expected and public.get('external_access') is False,
                '飞书权限 / 链接分享范围不符或外部访问未禁用；核对本文件设置后重试，未切换成功入口')
        if sharing_scope == 'tenant_readable':
            require(public.get('share_entity') == 'only_full_access',
                    '飞书权限 / 公司内只读看板须仅允许管理员管理协作者，未切换成功入口')
        # Check both direct and inherited folder grants; never silently allow a group/team.
        for resource, resource_type in ((self.folder,'folder'),(token,kind)):
            members = self.call(['drive','+member-list','--token',resource,'--type',resource_type])
            require(isinstance(members.get('items'),list) and not members.get('has_more'),
                    '飞书权限 / 成员清单不完整')
            require(all((m.get('member_type'),m.get('member_id')) in self.allowed for m in members['items']),
                    '飞书权限 / 存在未批准的直接或继承协作者，请到文件及父文件夹核对')

    def download(self, token):
        with tempfile.TemporaryDirectory() as directory:
            # HTML preview API support does not determine whether the web preview is interactive.
            data = self.call(['drive','+download','--file-token',token,'--output','source'],cwd=directory)
            saved = data.get('saved_path')
            require(isinstance(saved,str) and saved, '飞书完整性 / 下载响应缺少文件路径')
            path = Path(saved)
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
    _,data,_ = retained_data(source)
    require(data.get('analysis_version') == ANALYSIS_VERSION,
            '发布校验 / 历史批次口径需刷新；不能把历史交互预览作为当前口径的正式看板发布')
    if data.get('market_scope'):
        from market_scope import scope_config
        config = scope_config(config,data['market_scope']['markets'])
    sharing_scope = config.get('sharing_scope','admins_only')
    require(sharing_scope in ('admins_only','tenant_readable'), '飞书配置 / 分享范围只支持管理员或公司内只读')
    state_dir = Path(config['state_dir']); state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    state = state_dir/'last-success.json'
    candidate_path = state_dir/'candidate.json'
    remote = remote or Drive(config)
    if hasattr(remote, 'check_access'):
        access = remote.check_access()
        if not access['read_access_ready']:
            raise PublicationAccessError(access)
    if proof is not None:
        candidate = json.loads(candidate_path.read_text())
        require(candidate.get('sharing_scope','admins_only') == sharing_scope,
                '飞书验收 / 候选与当前分享范围不同，停止发布')
        require(candidate.get('manifest') == manifest(snapshot,data), '飞书验收 / 源数据或模板已变化，须重新准备候选')
        result = accept(remote,candidate,proof,state)
    else:
        require(not source.with_suffix('.status.json').exists(), '发布校验 / 最新取数失败，停止发布')
        _, data, _ = retained_data(source)
        require(not any(v != '响应无可用数据' for v in (data.get('platform') or {}).get('issues',{}).values()),
                '发布校验 / 平台快照读取失败，停止发布；请回 Answerbit 核查')
        version = manifest(snapshot,data)
        shared = shared_data(data)
        shared['delivery']['measurement_version'] = version['version']
        shared['delivery']['sharing_scope'] = sharing_scope
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory)/'share.html'; render(shared,html)
            result = stage(remote,data,version,html.read_bytes(),state,sharing_scope=sharing_scope)
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
    parser.add_argument('--check-access',action='store_true',help='只读检查发布连接并列出全部能力；不取数、不上传、不改权限')
    args = parser.parse_args(); config = {}
    try:
        config = load_config()
        if args.check_access:
            result = Drive(config).check_access()
            print(json.dumps(result,ensure_ascii=False))
            return 0 if result['read_access_ready'] else 1
        proof = json.loads(args.accept_proof.read_text()) if args.accept_proof else None
        result = publish(args.source,args.snapshot,config,proof)
        print(json.dumps(result,ensure_ascii=False)); return 0
    except (ArchiveError,OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        why = str(error) if isinstance(error,ArchiveError) else '文件、配置或响应结构异常；保留上一入口'
        batch = None
        try: batch = retained_data(args.source)[1]['batch']
        except (OSError,ValueError,KeyError,IndexError): pass
        result = failure(config,'飞书查重、上传或验收',why,batch)
        if hasattr(error,'publication_check'): result['publication_check'] = error.publication_check
        print(json.dumps(result,ensure_ascii=False)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
