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
from publication import CHECKS, manifest, stage, accept, report, initialize_registry


class Remote:
    registry_key = 'synthetic-approved-folder'

    def __init__(self):
        self.files = {}; self.names = {}; self.writes = 0; self.fail = None; self.sharing = {}

    def list_files(self):
        if self.fail == 'lookup': raise ArchiveError('查重失败')
        return [{'name':self.names[t],'token':t} for t in self.files]

    def download(self, token):
        if self.fail == 'download': raise ArchiveError('无法访问')
        return self.files[token]

    def verify_file(self, token, sharing_scope='admins_only'):
        if self.fail == 'permission': raise ArchiveError('权限检查失败')
        if self.sharing.get(token,'admins_only') != sharing_scope: raise ArchiveError('分享范围不符')

    def upload_private(self, body, name):
        if self.fail == 'upload': raise ArchiveError('上传中断')
        token = 'file'+str(len(self.files)); self.files[token] = body; self.names[token] = name
        self.writes += 1; return token

    def url(self, token): return 'https://example.feishu.cn/file/'+token

    def initialize(self, state):
        return initialize_registry(self,state,{'account':'synthetic-admin','checked_at':'2026-11-16T00:00:00Z',
                                              'history_checked':True,'receipt_tokens':[]})


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.AnswerbitTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.output.parent; self.source = self.root/'local.html'
        self.remote = Remote(); self.state = self.root/'last.json'
        self.remote.initialize(self.state)
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

    def test_old_evidence_stops_and_directory_permission_is_not_required(self):
        with sqlite3.connect(self.f.output) as db: db.execute('DROP TABLE source_evidence')
        with self.assertRaisesRegex(ArchiveError,'历史批次'): manifest(self.f.output,self.data)
        self.remote.fail = 'lookup'
        self.assertEqual(self.candidate()['status'],'pending_acceptance')

    def test_failures_do_not_replace_last_success(self):
        candidate = self.candidate(); accept(self.remote,candidate,self.proof(candidate),self.state)
        previous = self.state.read_bytes()
        self.detail['llm_output'] += ' change'; self.refresh()
        for failure in ('upload','permission','download'):
            self.remote.fail = failure
            with self.subTest(failure=failure), self.assertRaises(ArchiveError): self.candidate()
            self.assertEqual(self.state.read_bytes(),previous)
        self.remote.fail = None; candidate = self.candidate()
        bad = self.proof(candidate); bad['checks']['navigation'] = False
        with self.assertRaises(ArchiveError): accept(self.remote,candidate,bad,self.state)
        self.assertEqual(self.state.read_bytes(),previous)

    def test_shared_html_and_report_have_same_batch_without_raw_data(self):
        config = {'state_dir':str(self.root/'publication')}
        self.remote.initialize(Path(config['state_dir'])/'last-success.json')
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
        remote.state_dir = self.root
        remote.upload = lambda body,name,*args: remote.upload_private(body,name)
        remote.verify_file = lambda token: (_ for _ in ()).throw(ArchiveError('sharing open'))
        with self.assertRaises(ArchiveError): Drive.upload_private(remote,b'BUSINESS SECRET','geo-pending-test.html')
        self.assertEqual(list(remote.files.values()),[PLACEHOLDER])

    def test_incomplete_remote_receipt_stops_before_upload(self):
        token = self.remote.upload_private(b'{"accepted":true}', 'geo-accepted-old.json')
        writes = self.remote.writes
        with self.assertRaises(ArchiveError):
            initialize_registry(self.remote,self.root/'recovery'/'last-success.json',
                                {'account':'synthetic-admin','checked_at':'2026-11-16','history_checked':True,
                                 'receipt_tokens':[token]})
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
        self.remote.initialize(self.root/'end-to-end'/'markets-GB'/'last-success.json')
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

    def test_access_check_collects_all_read_failures_without_writes(self):
        drive = object.__new__(Drive); drive.folder = 'synthetic'; drive.identity = 'bot'
        with patch.object(drive,'call',side_effect=ArchiveError('错误码 99991672')) as call:
            result = drive.check_access()
        self.assertEqual(call.call_count,2)
        self.assertFalse(result['read_access_ready'])
        self.assertFalse(result['write_access_verified'])
        self.assertEqual([c['status'] for c in result['checks']],['blocked']*2+['not_tested']*5)
        self.assertEqual([c.args[0][:2] for c in call.call_args_list],
                         [['drive','+member-list'],['drive','+permission-get-setting']])
        self.assertFalse(result['directory_listing_required'])

    def test_read_access_success_does_not_claim_write_access(self):
        drive = object.__new__(Drive); drive.folder = 'synthetic'; drive.identity = 'user'
        replies = [{'items':[],'has_more':False},
                   {'permission_public':{'link_share_entity':'closed'}}]
        with patch.object(drive,'call',side_effect=replies):
            result = drive.check_access()
        self.assertTrue(result['read_access_ready'])
        self.assertFalse(result['write_access_verified'])
        self.assertEqual(result['identity'],'user')
        with patch.object(drive,'call',side_effect=[{},{}]):
            incomplete = drive.check_access()
        self.assertEqual([c['status'] for c in incomplete['checks'][:2]],['blocked','blocked'])

    def test_html_readback_downloads_exact_source_bytes_and_rejects_unsafe_paths(self):
        drive = object.__new__(Drive)
        body = b'<!doctype html><p>synthetic complete file</p>'
        def download(args,cwd=None):
            self.assertEqual(args,['drive','+download','--file-token','synthetic','--output','source'])
            path = Path(cwd)/'source'; path.write_bytes(body)
            return {'saved_path':str(path),'size_bytes':len(body)}
        with patch.object(drive,'call',side_effect=download):
            self.assertEqual(drive.download('synthetic'),body)
        for response in ({},{'saved_path':'../../outside'},{'saved_path':str(self.source)}):
            with self.subTest(response=response), patch.object(drive,'call',return_value=response), self.assertRaises(ArchiveError):
                drive.download('synthetic')

    def test_access_check_stops_publication_before_upload_and_keeps_previous_entry(self):
        from feishu_publish import PublicationAccessError
        config={'state_dir':str(self.root/'publication')}
        previous=Path(config['state_dir'])/'last-success.json'
        previous.parent.mkdir(); previous.write_text('{"url":"previous"}')
        original=previous.read_bytes()
        self.remote.check_access=lambda: {'read_access_ready':False,'checks':[{'status':'blocked'}]}
        with self.assertRaises(PublicationAccessError) as raised:
            publish(self.source,self.f.output,config,remote=self.remote)
        self.assertFalse(raised.exception.publication_check['read_access_ready'])
        self.assertEqual(self.remote.writes,0)
        self.assertEqual(previous.read_bytes(),original)

    def test_publishing_identity_defaults_without_asking_for_a_technical_field(self):
        config={'folder_url':'https://example.feishu.cn/drive/folder/synthetic',
                'approved_members':[{'member_type':'openid','member_id':'synthetic-admin'}]}
        with patch('feishu_publish.shutil.which',return_value='synthetic-cli'):
            for value in (None,''):
                with self.subTest(identity=value):
                    self.assertEqual(Drive(dict(config,identity=value)).identity,'bot')
            self.assertEqual(Drive(config).identity,'bot')
            self.assertEqual(Drive(dict(config,identity='user')).identity,'user')
            with self.assertRaises(ArchiveError): Drive(dict(config,identity='invalid'))

    def tenant_proof(self, candidate):
        proof = self.proof(candidate)
        proof.update(access_scope='tenant_verified',operator={
            'account':'synthetic-operator','checked_at':'2026-11-01T01:00:00Z',
            'checks':dict.fromkeys(CHECKS,True),
            'external_account':'synthetic-external','external_denied':True})
        return proof

    def test_company_link_needs_permission_and_independent_account_checks(self):
        candidate = stage(self.remote,self.data,self.version,b'synthetic shared html',self.state,
                          sharing_scope='tenant_readable')
        proof = self.tenant_proof(candidate)
        with self.assertRaisesRegex(ArchiveError,'分享范围'): accept(self.remote,candidate,proof,self.state)
        self.assertFalse(self.state.exists())
        self.remote.sharing[candidate['file_token']] = 'tenant_readable'
        for field,value in [('account',proof['account']),('external_denied',False),('external_account','')]:
            invalid = deepcopy(proof); invalid['operator'][field] = value
            with self.subTest(field=field), self.assertRaises(ArchiveError):
                accept(self.remote,candidate,invalid,self.state)
            self.assertFalse(self.state.exists())
        invalid = deepcopy(proof); invalid['operator']['checks']['navigation'] = False
        with self.assertRaises(ArchiveError): accept(self.remote,candidate,invalid,self.state)
        result = accept(self.remote,candidate,proof,self.state)
        self.assertEqual(result['status'],'published')
        receipts=[t for t,n in self.remote.names.items() if n.endswith('.json')]
        self.assertTrue(receipts)
        self.assertTrue(all(self.remote.sharing.get(t,'admins_only') == 'admins_only' for t in receipts))
        writes=self.remote.writes
        reused=stage(self.remote,self.data,self.version,b'synthetic shared html',self.state,
                     sharing_scope='tenant_readable')
        self.assertEqual(reused['status'],'reused');self.assertEqual(self.remote.writes,writes)

    def test_different_sharing_policies_never_reuse_or_overwrite_each_other(self):
        private=self.candidate();accept(self.remote,private,self.proof(private),self.state)
        previous=self.state.read_bytes()
        company=stage(self.remote,self.data,self.version,b'company html',self.state,sharing_scope='tenant_readable')
        self.assertEqual(company['status'],'pending_acceptance')
        self.assertNotEqual(company['file_token'],private['file_token'])
        self.assertEqual(self.state.read_bytes(),previous)
        with self.assertRaises(ArchiveError): accept(self.remote,company,self.proof(company),self.state)
        self.assertEqual(self.state.read_bytes(),previous)
        for mode in ('anyone_readable','tenant_editable','invalid'):
            writes=self.remote.writes
            with self.assertRaises(ArchiveError):
                stage(self.remote,self.data,self.version,b'html',self.state,sharing_scope=mode)
            self.assertEqual(self.remote.writes,writes)

    def test_company_mode_still_rejects_external_access_and_private_manifest_exposure(self):
        drive=object.__new__(Drive);drive.folder='folder';drive.allowed={('openid','admin')}
        members={'items':[{'member_type':'openid','member_id':'admin'}]}
        public={'permission_public':{'link_share_entity':'tenant_readable','external_access':False,
                                     'share_entity':'only_full_access'}}
        with patch.object(drive,'call',side_effect=[public,members,members]):
            drive.verify_file('file',sharing_scope='tenant_readable')
        for mode,external in [('tenant_readable',True),('tenant_readable',None),('anyone_readable',False),('tenant_editable',False)]:
            response={'permission_public':{'link_share_entity':mode,'external_access':external}}
            with patch.object(drive,'call',return_value=response),self.assertRaises(ArchiveError):
                drive.verify_file('file',sharing_scope='tenant_readable')
        with patch.object(drive,'call',return_value=public),self.assertRaises(ArchiveError):
            drive.verify_file('receipt')
        for management in ('anyone','same_tenant',None):
            unsafe=deepcopy(public);unsafe['permission_public']['share_entity']=management
            with patch.object(drive,'call',return_value=unsafe),self.assertRaises(ArchiveError):
                drive.verify_file('file',sharing_scope='tenant_readable')

    def test_company_receipt_visibility_change_stops_reuse(self):
        candidate=stage(self.remote,self.data,self.version,b'html',self.state,sharing_scope='tenant_readable')
        self.remote.sharing[candidate['file_token']]='tenant_readable'
        accept(self.remote,candidate,self.tenant_proof(candidate),self.state)
        previous=self.state.read_bytes();writes=self.remote.writes
        receipt=next(t for t,n in self.remote.names.items() if n.endswith('.json'))
        self.remote.sharing[receipt]='tenant_readable'
        with self.assertRaises(ArchiveError):
            stage(self.remote,self.data,self.version,b'html',self.state,sharing_scope='tenant_readable')
        self.assertEqual(self.remote.writes,writes);self.assertEqual(self.state.read_bytes(),previous)

    def test_accept_rechecks_config_and_source_before_publishing(self):
        config={'state_dir':str(self.root/'publication'),'sharing_scope':'tenant_readable'}
        self.remote.initialize(Path(config['state_dir'])/'last-success.json')
        candidate=publish(self.source,self.f.output,config,remote=self.remote)
        proof=self.tenant_proof(candidate)
        self.remote.sharing[candidate['file_token']]='tenant_readable'
        with self.assertRaisesRegex(ArchiveError,'分享范围不同'):
            publish(self.source,self.f.output,dict(config,sharing_scope='admins_only'),proof,self.remote)
        self.detail['llm_output']+=' changed source';self.refresh()
        with self.assertRaisesRegex(ArchiveError,'源数据或模板已变化'):
            publish(self.source,self.f.output,config,proof,self.remote)
        self.assertFalse((Path(config['state_dir'])/'last-success.json').exists())

    def test_legacy_analysis_cannot_publish_a_report_that_disagrees_with_current_metrics(self):
        old=deepcopy(self.data);old['analysis_version']='historical-policy'
        dashboard.render(old,self.source)
        config={'state_dir':str(self.root/'publication'),'sharing_scope':'tenant_readable'}
        with self.assertRaisesRegex(ArchiveError,'历史批次口径需刷新'):
            publish(self.source,self.f.output,config,remote=self.remote)
        self.assertEqual(self.remote.writes,0)
        self.assertFalse((Path(config['state_dir'])/'last-success.json').exists())

    def test_missing_corrupt_or_wrong_target_registry_never_uploads(self):
        path = self.state.with_name('registry.json'); original = path.read_bytes()
        wrong = json.loads(original); wrong['target'] = 'another-folder'
        for body in (None,b'{',json.dumps(wrong).encode()):
            if body is None: path.unlink(missing_ok=True)
            else: path.write_bytes(body)
            with self.subTest(body=body), self.assertRaises(ArchiveError): self.candidate()
            self.assertEqual(self.remote.writes,0)
        path.write_bytes(original)
        with self.assertRaises(ArchiveError): self.remote.initialize(self.state)

    def test_bootstrap_imports_known_receipts_and_never_erases_previous(self):
        candidate = self.candidate(); accept(self.remote,candidate,self.proof(candidate),self.state)
        previous = self.state.read_bytes()
        token = next(t for t,n in self.remote.names.items() if n.endswith('.json'))
        path = self.state.with_name('registry.json'); path.unlink()
        with self.assertRaises(ArchiveError): self.candidate()
        with self.assertRaises(ArchiveError): self.remote.initialize(self.state)
        self.assertEqual(self.state.read_bytes(),previous)
        proof={'account':'synthetic-admin','checked_at':'2026-11-16','history_checked':False,'receipt_tokens':[token]}
        with self.assertRaises(ArchiveError): initialize_registry(self.remote,self.state,proof)
        self.assertFalse(path.exists())
        proof['history_checked']=True
        writes=self.remote.writes
        initialize_registry(self.remote,self.state,proof)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.remote.fail='lookup'
        with patch.object(self.remote,'list_files',side_effect=AssertionError('directory must not be read')):
            self.assertEqual(self.candidate()['status'],'reused')
        self.assertEqual(self.remote.writes,writes)

    def test_new_version_and_accept_retry_use_only_registered_files(self):
        with patch.object(self.remote,'list_files',side_effect=AssertionError('directory must not be read')):
            candidate=self.candidate(); first=accept(self.remote,candidate,self.proof(candidate),self.state)
            writes=self.remote.writes
            self.assertEqual(accept(self.remote,candidate,self.proof(candidate),self.state),first)
            self.assertEqual(self.remote.writes,writes)
            previous=self.state.read_bytes()
            self.detail['llm_output']+=' Updated claim.';self.refresh()
            new=self.candidate()
            self.assertNotEqual(new['file_token'],candidate['file_token'])
            self.assertEqual(self.state.read_bytes(),previous)
            accept(self.remote,new,self.proof(new),self.state)
            registry=json.loads(self.state.with_name('registry.json').read_text())
            self.assertEqual(len(registry['records']),2)

    def test_upload_journal_resumes_known_token_and_stops_unknown_outcome(self):
        remote=Remote();remote.state_dir=self.root/'uploads'
        body=b'approved synthetic dashboard'
        def upload(content,name,token=None):
            if token:
                remote.files[token]=content;remote.writes+=1
                raise ArchiveError('response lost after body update')
            return remote.upload_private(content,name)
        remote.upload=upload
        with self.assertRaises(ArchiveError): Drive.upload_private(remote,body,'candidate.html')
        writes=remote.writes
        token=Drive.upload_private(remote,body,'candidate.html')
        self.assertEqual(remote.download(token),body);self.assertEqual(remote.writes,writes)
        self.assertEqual(len(remote.files),1)
        def lost(content,name,token=None):
            remote.upload_private(content,name)
            raise ArchiveError('response lost before token recorded')
        remote.upload=lost
        with self.assertRaises(ArchiveError): Drive.upload_private(remote,body,'unknown.html')
        writes=remote.writes
        with self.assertRaisesRegex(ArchiveError,'结果不明'):
            Drive.upload_private(remote,body,'unknown.html')
        self.assertEqual(remote.writes,writes)
        self.assertEqual(list(remote.files.values()).count(PLACEHOLDER),1)

    def test_first_bootstrap_needs_no_existing_dashboard_or_data_fetch(self):
        import contextlib
        import io
        import feishu_publish
        config={'state_dir':str(self.root/'new-central')}
        proof=self.root/'review.json'
        proof.write_text(json.dumps({'account':'synthetic-admin','checked_at':'2026-11-16',
                                     'history_checked':True,'receipt_tokens':[]}))
        argv=['feishu_publish.py',str(self.root/'not-created.html'),str(self.root/'not-created.sqlite'),
              '--market','美国','--initialize-registry',str(proof)]
        with patch('sys.argv',argv), patch.object(feishu_publish,'load_config',return_value=config), \
             patch.object(feishu_publish,'Drive',return_value=self.remote), \
             patch.object(answerbit,'operator_credentials',side_effect=AssertionError('no Answerbit')), \
             patch.object(self.remote,'list_files',side_effect=AssertionError('no directory scan')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(feishu_publish.main(),0)
        registry=self.root/'new-central'/'markets-US'/'registry.json'
        self.assertTrue(registry.is_file());self.assertEqual(self.remote.writes,0)
        self.assertFalse((registry.parent/'last-success.json').exists())

    def test_interrupted_success_pointer_recovers_from_registered_receipt(self):
        import publication
        candidate=self.candidate();accept(self.remote,candidate,self.proof(candidate),self.state)
        previous=self.state.read_bytes()
        self.detail['llm_output']+=' new evidence';self.refresh();new=self.candidate()
        original=publication.write_atomic
        def fail_pointer(path,body):
            if path == self.state: raise OSError('synthetic pointer interruption')
            original(path,body)
        with patch.object(publication,'write_atomic',side_effect=fail_pointer),self.assertRaises(OSError):
            accept(self.remote,new,self.proof(new),self.state)
        self.assertEqual(self.state.read_bytes(),previous)
        writes=self.remote.writes
        recovered=self.candidate()
        self.assertEqual(recovered['status'],'reused')
        self.assertEqual(recovered['receipt']['file_token'],new['file_token'])
        self.assertEqual(self.remote.writes,writes)

    def test_accept_upload_retry_keeps_the_exact_receipt_bytes(self):
        candidate=self.candidate(); bodies=[]; original=self.remote.upload_private
        def interrupted(body,name):
            if name.endswith('.json'):
                bodies.append(body)
                if len(bodies)==1: raise ArchiveError('synthetic receipt upload interrupted')
            return original(body,name)
        self.remote.upload_private=interrupted
        with self.assertRaises(ArchiveError): accept(self.remote,candidate,self.proof(candidate),self.state)
        self.assertFalse(self.state.exists())
        accept(self.remote,candidate,self.proof(candidate),self.state)
        self.assertEqual(bodies[0],bodies[1])
