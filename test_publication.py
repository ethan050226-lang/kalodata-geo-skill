from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import answerbit
import dashboard
import test_answerbit as fixture
from archive import ArchiveError
from feishu_delivery import shared_data
from feishu_publish import Drive, PLACEHOLDER, publish
from publication import CHECKS, manifest, stage, accept, report


class Remote:
    def __init__(self):
        self.files = {}; self.names = {}; self.writes = 0; self.fail = None

    def list_files(self):
        if self.fail == 'lookup': raise ArchiveError('查重失败')
        return [{'name':self.names[t],'token':t} for t in self.files]

    def download(self, token):
        if self.fail == 'download': raise ArchiveError('无法访问')
        return self.files[token]

    def verify_file(self, token):
        if self.fail == 'permission': raise ArchiveError('权限检查失败')

    def upload_private(self, body, name):
        if self.fail == 'upload': raise ArchiveError('上传中断')
        token = 'file'+str(len(self.files)); self.files[token] = body; self.names[token] = name
        self.writes += 1; return token

    def url(self, token): return 'https://example.feishu.cn/file/'+token


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.AnswerbitTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.output.parent; self.source = self.root/'local.html'
        self.remote = Remote(); self.state = self.root/'last.json'
        self.record = self.f.records()[0]
        self.detail = dict(prompt=self.record['query_str'],platform=self.record['platform'],
                           language='en',date=self.f.day,links=[],brand_reputation=1,
                           llm_output='Kalodata and FastMoss. Original private answer.')
        self.refresh()

    def refresh(self, brand='test'):
        def call(path, payload):
            return {'scores':[self.record],'total':1} if path == answerbit.LIST else self.detail
        dashboard.refresh_answers(self.f.bank,self.f.output,brand,self.f.day,self.f.day,call)
        self.data = dashboard.dashboard_data(self.f.output,self.f.bank,None)
        self.version = manifest(self.f.output,self.data)
        dashboard.render(self.data,self.source)

    def candidate(self):
        return stage(self.remote,self.data,self.version,b'synthetic html',self.state)

    def proof(self, candidate):
        return {'file_token':candidate['file_token'],'html_sha256':candidate['html_sha256'],
                'checks':dict.fromkeys(CHECKS,True),'account':'approved-admin',
                'access_scope':'admins_only','checked_at':'2026-11-01T00:00:00Z'}

    def test_identical_version_reuses_only_after_full_remote_readback(self):
        candidate = self.candidate(); self.assertFalse(self.state.exists())
        accepted = accept(self.remote,candidate,self.proof(candidate),self.state)
        writes = self.remote.writes
        self.refresh()  # fresh timestamps and new SQLite bytes must not force a new version
        reused = self.candidate()
        self.assertEqual(reused['status'],'reused'); self.assertEqual(self.remote.writes,writes)
        self.assertEqual(reused['receipt']['url'],accepted['receipt']['url'])
        self.assertEqual(reused['receipt']['report']['metrics'],report(self.data)['metrics'])
        self.assertEqual(reused['receipt']['report']['batch'],accepted['receipt']['report']['batch'])
        self.remote.files[candidate['file_token']] += b'tampered'
        with self.assertRaises(ArchiveError): self.candidate()

    def test_same_ids_and_counts_do_not_hide_content_source_or_sentiment_changes(self):
        original = self.version
        self.detail['llm_output'] += ' Another factual claim, unchanged brand counts.'
        self.refresh(); self.assertNotEqual(self.version['version'],original['version'])
        self.assertEqual(self.version['sample_ids_sha256'],original['sample_ids_sha256'])
        previous = self.version
        self.detail['links'] = [{'url':'https://www.kalodata.com/new-source'}]
        self.refresh(); self.assertNotEqual(self.version['version'],previous['version'])
        previous = self.version
        self.detail['links'] = [{'url':'https://www.kalodata.com/another-source'}]
        self.refresh(); self.assertNotEqual(self.version['version'],previous['version'])
        previous = self.version
        self.detail['analysis'] = {'state':2,'verification_note':'changed'}
        self.refresh(); self.assertNotEqual(self.version['version'],previous['version'])
        previous = self.version
        self.detail['brand_reputation'] = 3
        self.refresh(); self.assertNotEqual(self.version['version'],previous['version'])
        previous = self.version
        self.refresh(brand='another-brand'); self.assertNotEqual(self.version['version'],previous['version'])
        previous = self.version
        self.record['tags'].append({'tag_id':'new','tag_name':'new','tag_type':2})
        self.refresh(); self.assertNotEqual(self.version['version'],previous['version'])

    def test_old_evidence_and_failed_lookup_never_mean_same_version(self):
        with sqlite3.connect(self.f.output) as db: db.execute('DROP TABLE source_evidence')
        with self.assertRaisesRegex(ArchiveError,'历史批次'): manifest(self.f.output,self.data)
        self.remote.fail = 'lookup'
        with self.assertRaises(ArchiveError): self.candidate()
        self.assertEqual(self.remote.writes,0)

    def test_failures_do_not_replace_last_success(self):
        candidate = self.candidate(); accept(self.remote,candidate,self.proof(candidate),self.state)
        previous = self.state.read_bytes()
        self.detail['llm_output'] += ' change'; self.refresh()
        for failure in ('lookup','upload','permission','download'):
            self.remote.fail = failure
            with self.subTest(failure=failure), self.assertRaises(ArchiveError): self.candidate()
            self.assertEqual(self.state.read_bytes(),previous)
        self.remote.fail = None; candidate = self.candidate()
        bad = self.proof(candidate); bad['checks']['navigation'] = False
        with self.assertRaises(ArchiveError): accept(self.remote,candidate,bad,self.state)
        self.assertEqual(self.state.read_bytes(),previous)

    def test_shared_html_and_report_have_same_batch_without_raw_data(self):
        config = {'state_dir':str(self.root/'publication')}
        result = publish(self.source,self.f.output,config,remote=self.remote)
        body = self.remote.download(result['file_token'])
        self.assertNotIn(b'Original private answer',body)
        self.assertNotIn(b'127.0.0.1/',body)
        self.assertEqual(result['report']['batch'],self.data['batch'])
        metric = next(m for m in result['report']['metrics'] if m['id']=='skill.core_mention_rate')
        self.assertEqual((metric['numerator'],metric['denominator']),(1,1))
        self.source.with_suffix('.status.json').write_text('{}')
        writes = self.remote.writes
        with self.assertRaises(ArchiveError): publish(self.source,self.f.output,config,remote=self.remote)
        self.assertEqual(self.remote.writes,writes)

    def test_private_upload_never_sends_business_data_before_permission_check(self):
        remote = Remote()
        remote.upload = lambda body,name,*args: remote.upload_private(body,name)
        remote.verify_file = lambda token: (_ for _ in ()).throw(ArchiveError('sharing open'))
        with self.assertRaises(ArchiveError): Drive.upload_private(remote,b'BUSINESS SECRET','geo-pending-test.html')
        self.assertEqual(list(remote.files.values()),[PLACEHOLDER])

    def test_incomplete_remote_receipt_stops_before_upload(self):
        self.remote.upload_private(b'{"accepted":true}', 'geo-accepted-old.json')
        writes = self.remote.writes
        with self.assertRaises(ArchiveError): self.candidate()
        self.assertEqual(self.remote.writes,writes)

    def test_version_tracks_bank_scope_metric_and_ids(self):
        previous = self.version
        self.record['task_id'] = 'another-id'; self.refresh()
        self.assertNotEqual(previous['sample_ids_sha256'],self.version['sample_ids_sha256'])
        for key in ('analysis_version',):
            changed = deepcopy(self.data); changed[key] = 'new-policy'
            self.assertNotEqual(manifest(self.f.output,changed)['version'],self.version['version'])
        changed = deepcopy(self.data); changed['rows'][0]['d'] = ('GB','changed-platform',self.f.day,'other-group',1,('new-tag',))
        self.assertNotEqual(manifest(self.f.output,changed)['version'],self.version['version'])
        previous = self.version
        from openpyxl import load_workbook
        book = load_workbook(self.f.bank); book['英国']['A2'] = 'changed category'; book.save(self.f.bank); book.close()
        self.refresh(); self.assertNotEqual(previous['bank_sha256'],self.version['bank_sha256'])

    def test_default_run_waits_for_key_and_never_publishes_failed_data(self):
        import geo_run
        self.source.with_suffix('.status.json').write_text('{"stage":"old","error":"stale failure"}')
        with patch.object(geo_run,'load_config',return_value={'state_dir':str(self.root)}), \
             patch.dict(os.environ,{'GEO_QUESTION_BANK_URL':'https://example.feishu.cn/wiki/test'}), \
             patch.object(answerbit,'operator_credentials',side_effect=ArchiveError('凭证缺失')), \
             patch('feishu_bank.fetch_bank') as fetch, patch.object(geo_run,'publish') as publish_call:
            result, code = geo_run.run(self.f.bank,self.f.output,self.source,markets=['GB'])
        self.assertEqual(code,1); self.assertEqual(result['status'],'failed')
        self.assertIn('凭证缺失',result['reason']); self.assertNotIn('stale',result['reason'])
        self.assertEqual(len(result['affected_batch']),2)
        fetch.assert_not_called(); publish_call.assert_not_called()

    def test_proof_cannot_upload_extra_private_fields(self):
        candidate = self.candidate(); proof = self.proof(candidate); proof['api_key'] = 'private'
        writes = self.remote.writes
        with self.assertRaises(ArchiveError): accept(self.remote,candidate,proof,self.state)
        self.assertEqual(self.remote.writes,writes)

    def test_permission_unknown_or_unapproved_member_fails_closed(self):
        drive = object.__new__(Drive); drive.folder = 'folder'
        drive.allowed = {('openid','admin')}
        with patch.object(drive,'call',return_value={'permission_public':{'link_share_entity':'closed'}}):
            with self.assertRaisesRegex(ArchiveError,'外部访问'): drive.verify_file('file')
        responses = [{'permission_public':{'link_share_entity':'closed','external_access':False}},
                     {'items':[{'member_type':'openid','member_id':'outsider'}]}]
        with patch.object(drive,'call',side_effect=responses):
            with self.assertRaisesRegex(ArchiveError,'未批准'): drive.verify_file('file')

    def test_download_failure_on_old_link_never_creates_duplicate(self):
        candidate = self.candidate(); accept(self.remote,candidate,self.proof(candidate),self.state)
        writes = self.remote.writes; old = self.state.read_bytes(); self.remote.fail = 'download'
        with self.assertRaises(ArchiveError): self.candidate()
        self.assertEqual(self.remote.writes,writes); self.assertEqual(self.state.read_bytes(),old)

    def test_default_run_through_acceptance_then_reuse(self):
        import geo_run
        import feishu_publish
        config = {'state_dir':str(self.root/'end-to-end'), 'folder_url':'https://example.feishu.cn/drive/folder/synthetic',
                  'approved_members':[{'member_type':'openid','member_id':'synthetic-admin'}], 'identity':'bot'}
        def call(path,payload):
            return {'scores':[self.record],'total':1} if path == answerbit.LIST else self.detail
        with patch.object(geo_run,'load_config',return_value=config), \
             patch.dict(os.environ,{'GEO_QUESTION_BANK_URL':'https://example.feishu.cn/wiki/test'}), \
             patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','test')), \
             patch('feishu_bank.fetch_bank',return_value={'source':'feishu-live','revision':1}), \
             patch.object(dashboard,'PacedRead',return_value=call), \
             patch.object(dashboard,'platform_snapshot',return_value={'data':{},'issues':{}}), \
             patch.object(feishu_publish,'Drive',return_value=self.remote):
            options = ['--begin',self.f.day,'--end',self.f.day]
            candidate, code = geo_run.run(self.f.bank,self.f.output,self.source,options,markets=['GB'])
            self.assertEqual((code,candidate['status']),(0,'pending_acceptance'))
            published = publish(self.source,self.f.output,config,self.proof(candidate),self.remote)
            self.assertEqual(published['status'],'published')
            writes = self.remote.writes
            reused, code = geo_run.run(self.f.bank,self.f.output,self.source,options,markets=['GB'])
            self.assertEqual((code,reused['status']),(0,'reused'))
            self.assertEqual(self.remote.writes,writes)
            self.assertEqual(published['receipt']['report'],reused['receipt']['report'])
        self.assertFalse(any(name.endswith('.xlsx') for name in self.remote.names.values()))

    def test_cli_malformed_success_cannot_be_mistaken_for_valid_result(self):
        from types import SimpleNamespace
        drive = object.__new__(Drive); drive.cli = 'official-cli'; drive.identity = 'bot'
        for stdout in ('[]','{"ok":true,"data":null}'):
            result = SimpleNamespace(returncode=0,stdout=stdout,stderr='')
            with patch('feishu_publish.subprocess.run',return_value=result), self.assertRaises(ArchiveError):
                drive.call(['drive','files','list'])
