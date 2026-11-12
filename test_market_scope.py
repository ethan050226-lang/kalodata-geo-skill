from copy import deepcopy
from io import BytesIO
import contextlib
import io
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

import answerbit
import dashboard
import geo_run
import test_answerbit as fixture
from archive import ArchiveError
from feishu_delivery import shared_data
from feishu_publish import publish
from market_scope import normalize_markets, restrict_snapshot, scope_config
from publication import CHECKS, accept, manifest, report
from test_publication import Remote
from workbook_export import export_workbook


class CountryDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.AnswerbitTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.output.parent
        book = load_workbook(self.f.bank)
        book['美国']['C2'] = 'US_ONLY_QUESTION'
        book.save(self.f.bank); book.close()
        self.gb = self.f.records()[0]
        self.us = deepcopy(self.gb)
        self.us.update(task_id='us-only-task',query_str='US_ONLY_QUESTION',title_id='us-only-group',title_name='US_ONLY_GROUP',
                       tags=[{'tag_id':'us-only-tag','tag_name':'美国','tag_type':1}])
        self.records = [self.gb,self.us]
        self.scoped = self.root/'country.sqlite'
        self.html = self.root/'country.html'
        self.excel = self.root/'country.xlsx'
        self.refresh()

    def call(self,path,payload):
        if path == answerbit.LIST:
            return {'scores':self.records,'total':len(self.records)}
        record = next(r for r in self.records if r['task_id']==payload['task_id'])
        return {'prompt':record['query_str'],'platform':record['platform'],'language':record['language'],'date':self.f.day,
                'links':[], 'brand_reputation':2 if record is self.gb else 1,
                'llm_output':'Kalodata. '+('GB_ORIGINAL' if record is self.gb else 'US_ORIGINAL')}

    def refresh(self):
        dashboard.refresh_answers(self.f.bank,self.f.output,'synthetic',self.f.day,self.f.day,self.call)

    def scope(self,markets=('GB',)):
        restrict_snapshot(self.f.output,self.scoped,list(markets))
        return dashboard.dashboard_data(self.scoped,self.f.bank,{'data':{'full':'PRIVATE_FULL_PLATFORM'}})

    def test_scope_recomputes_and_removes_other_countries_from_every_artifact(self):
        data = self.scope()
        self.assertEqual(data['market_scope']['markets'],['GB'])
        self.assertEqual(set(data['markets']),{'GB'})
        self.assertEqual(data['core_mention'],{'numerator':0,'denominator':1,'excluded_negative':1,'value':0})
        self.assertIsNone(data['platform'])
        self.assertEqual(data['batch']['returned_records'],1)
        dashboard.render(shared_data(data),self.html)
        book = load_workbook(BytesIO(export_workbook(self.scoped,data)))
        cells = [str(c.value) for sheet in book for row in sheet for c in row]
        book.close()
        exported = self.html.read_text()+json.dumps(report(data))+str(cells)
        for marker in ('US_ONLY_QUESTION','US_ONLY_GROUP','us-only-task','us-only-tag','PRIVATE_FULL_PLATFORM','US_ORIGINAL','GB_ORIGINAL'):
            self.assertNotIn(marker,exported)
        self.assertNotIn(b'US_ORIGINAL',self.scoped.read_bytes())
        self.assertNotIn(b'US_ONLY_QUESTION',self.scoped.read_bytes())
        self.assertEqual(manifest(self.scoped,data)['sample_count'],1)
        with sqlite3.connect(self.scoped) as db:
            self.assertEqual(db.execute('SELECT body FROM sample_answer').fetchone()[0],'Kalodata. GB_ORIGINAL')
        self.assertEqual(cells.count('英国'),1)

    def test_invalid_empty_and_cross_country_candidate_block_delivery(self):
        for markets in ([],['XX']):
            with self.assertRaises(ArchiveError): normalize_markets(markets)
        self.assertEqual(normalize_markets(['英国','GB','美国']),['GB','US'])
        with self.assertRaisesRegex(ArchiveError,'没有有效回答'): self.scope(('DE',))
        with sqlite3.connect(self.f.output) as db:
            db.execute('INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?)',
                       ('ambiguous',None,'demo',self.f.day,'ambiguous_question','核查','Shared question?','TEST-01-GB、TEST-01-US',None))
        with self.assertRaisesRegex(ArchiveError,'候选编号'): self.scope()

    def test_multicountry_and_shared_boundary_rejects_tampered_scope(self):
        data = self.scope(('GB','US'))
        self.assertEqual(data['core_mention']['numerator'],1)
        self.assertEqual(data['core_mention']['denominator'],2)
        tampered = deepcopy(data); tampered['market_scope']['markets']=['GB']
        with self.assertRaisesRegex(ArchiveError,'国家范围'): shared_data(tampered)
        with self.assertRaises(ArchiveError): manifest(self.scoped,tampered)
        tampered=deepcopy(data)
        tampered['comparison']={'previous_batch':data['batch'],'delivery_markets':['GB','US'],'previous_delivery_markets':['CN']}
        with self.assertRaisesRegex(ArchiveError,'上次对比'): shared_data(tampered)

    def test_missing_live_bank_blocks_before_answerbit_even_with_local_export(self):
        with patch.dict(os.environ,{},clear=True), \
             patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')), \
             patch.object(answerbit,'question_bank_url',return_value=None), \
             patch.object(dashboard,'refresh_answers') as pull, \
             patch.object(geo_run,'load_config',side_effect=ArchiveError('飞书配置缺失')), \
             patch.object(geo_run,'publish') as upload, \
             patch('feishu_bank.fetch_bank') as bank_read, contextlib.redirect_stdout(io.StringIO()):
            result,code = geo_run.run(self.f.bank,self.scoped,self.html,
                ['--begin',self.f.day,'--end',self.f.day,'--excel',str(self.excel)],markets=['英国'])
        self.assertEqual((code,result['status']),(1,'failed'))
        self.assertIn('在线题库',result['reason'])
        self.assertFalse(self.html.exists() or self.excel.exists())
        pull.assert_not_called()
        upload.assert_not_called(); bank_read.assert_not_called()
        with patch.object(dashboard,'main') as run:
            failure,code = geo_run.run(self.f.bank,self.scoped,self.html)
        self.assertEqual(code,1); run.assert_not_called()

    def test_failure_preserves_same_country_previous_and_other_country_baseline_is_not_embedded(self):
        def snapshot(markets):
            with contextlib.redirect_stdout(io.StringIO()):
                return dashboard.main(['snapshot',str(self.f.bank),str(self.f.output),str(self.html),
                                       '--excel',str(self.excel),*sum((['--market',m] for m in markets),[])])
        # The old HTML is full-brand. Scoping must discard its history as well as current rows.
        old = dashboard.dashboard_data(self.f.output,self.f.bank,None)
        old['batch']['fetched_at_utc']='2000-01-01T00:00:00Z'
        dashboard.render(old,self.html)
        self.assertEqual(snapshot(['GB']),0)
        country = dashboard.retained_data(self.html)[1]
        self.assertIsNone(country['comparison']['previous_batch'])
        self.assertTrue(country['comparison']['limitations'])
        self.assertNotIn('US_ONLY',self.html.read_text())
        before_db,before_excel = self.f.output.read_bytes(),self.excel.read_bytes()
        original = answerbit.write_atomic
        failed = False
        def fail_excel(path,content):
            nonlocal failed
            if path == self.excel and not failed:
                failed = True
                raise OSError('synthetic write interruption')
            return original(path,content)
        with patch.object(answerbit,'write_atomic',side_effect=fail_excel):
            self.assertEqual(snapshot(['GB']),1)
        self.assertEqual(self.f.output.read_bytes(),before_db)
        self.assertEqual(self.excel.read_bytes(),before_excel)
        self.assertEqual(dashboard.retained_data(self.html)[1]['batch'],country['batch'])
        self.assertTrue(self.html.with_suffix('.status.json').is_file())

    def test_country_publication_uses_same_batch_and_keeps_previous_on_upload_failure(self):
        data = self.scope(); dashboard.render(data,self.html)
        remote = Remote(); config={'state_dir':str(self.root/'publication')}
        candidate=publish(self.html,self.scoped,config,remote=remote)
        state=self.root/'publication'/'markets-GB'/'last-success.json'
        proof={'file_token':candidate['file_token'],'html_sha256':candidate['html_sha256'],
               'checks':dict.fromkeys(CHECKS,True),'account':'synthetic-admin','access_scope':'admins_only','checked_at':'2026-11-01'}
        accepted=accept(remote,candidate,proof,state)
        self.assertEqual(accepted['receipt']['report']['batch'],data['batch'])
        self.assertNotIn(b'US_ONLY',remote.download(candidate['file_token']))
        saved=state.read_bytes()
        self.gb['title_name']='changed';self.refresh();data=self.scope();dashboard.render(data,self.html)
        remote.fail='upload'
        with self.assertRaises(ArchiveError):publish(self.html,self.scoped,config,remote=remote)
        self.assertEqual(state.read_bytes(),saved)
        self.assertNotEqual(scope_config(config,['US'])['state_dir'],str(state.parent))

    def test_preflight_never_loads_private_publishing_configuration_before_key(self):
        with patch.object(answerbit,'operator_credentials',side_effect=ArchiveError('缺少Key')), \
             patch.object(geo_run,'load_config') as private:
            with self.assertRaises(ArchiveError): geo_run.preflight(self.f.bank,['GB'])
        private.assert_not_called()
        with patch.dict(os.environ,{},clear=True), \
             patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','private-brand')), \
             patch.object(answerbit,'question_bank_url',return_value=None), \
             patch.object(geo_run,'load_config',side_effect=ArchiveError('not ready')):
            checked=geo_run.preflight(self.f.bank,['GB'])
        self.assertEqual(len(checked['blockers']),2)
        self.assertNotIn('private-brand',json.dumps(checked))
        self.assertNotIn('TEST_ONLY',json.dumps(checked))

    def test_private_bank_url_drives_online_refresh_without_environment_url(self):
        config=self.root/'private-resources.json'
        url='https://example.feishu.cn/wiki/synthetic-bank'
        config.write_text(json.dumps({'brand_id':'synthetic-brand','team_id':'synthetic-team','question_bank_url':url}))
        with patch.dict(os.environ,{'ANSWERBIT_API_KEY':'TEST_ONLY','ANSWERBIT_RESOURCE_CONFIG':str(config)},clear=True), \
             patch('feishu_bank.fetch_bank',return_value={'source':'feishu-live','revision':1}) as fetch, \
             patch.object(dashboard,'PacedRead',return_value=self.call), \
             patch.object(geo_run,'load_config',side_effect=ArchiveError('发布配置未就绪')), \
             patch.object(geo_run,'publish') as upload, contextlib.redirect_stdout(io.StringIO()):
            result,code=geo_run.run(self.f.bank,self.scoped,self.html,['--begin',self.f.day,'--end',self.f.day],markets=['GB'])
        self.assertEqual((code,result['status']),(0,'local_ready_pending_publication'))
        fetch.assert_called_once_with(url,self.f.bank)
        self.assertEqual(len(result['blockers']),1)
        upload.assert_not_called()
        public=self.html.read_text()
        for marker in ('synthetic-brand','synthetic-team','TEST_ONLY'):
            self.assertNotIn(marker,public)

    def test_all_live_modes_stop_before_answerbit_when_bank_read_fails(self):
        for mode,extra in (('refresh',[]),('review',[]),('snapshot',['--fetch-platform'])):
            with self.subTest(mode=mode), \
                 patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')), \
                 patch.object(answerbit,'question_bank_url',return_value='https://example.feishu.cn/wiki/test'), \
                 patch('feishu_bank.fetch_bank',side_effect=ArchiveError('在线题库读取失败')) as bank, \
                 patch.object(dashboard,'refresh_answers') as pull, \
                 patch.object(dashboard,'run_review') as review, \
                 patch.object(dashboard,'platform_snapshot') as platform, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dashboard.main([mode,str(self.f.bank),str(self.f.output),str(self.html),*extra]),1)
                bank.assert_called_once();pull.assert_not_called();review.assert_not_called();platform.assert_not_called()

    def test_history_review_rejects_changed_live_bank_without_rewriting_history(self):
        import shutil
        def latest(url,destination):
            shutil.copyfile(self.f.bank,destination)
            book=load_workbook(destination);book['英国']['C2']='Changed live question';book.save(destination);book.close()
            return {'source':'feishu-live','revision':2}
        old_db,old_bank=self.f.output.read_bytes(),self.f.bank.read_bytes()
        with patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')), \
             patch.object(answerbit,'question_bank_url',return_value='https://example.feishu.cn/wiki/test'), \
             patch('feishu_bank.fetch_bank',side_effect=latest), \
             patch.object(dashboard,'run_review') as review,contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(dashboard.main(['review',str(self.f.bank),str(self.f.output),str(self.html)]),1)
        review.assert_not_called()
        self.assertIn('在线题库已不同于历史题库',log.getvalue())
        self.assertEqual(old_db,self.f.output.read_bytes());self.assertEqual(old_bank,self.f.bank.read_bytes())

    def test_offline_bank_flag_cannot_bypass_live_gate(self):
        with patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')), \
             patch.object(dashboard,'refresh_answers') as pull,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dashboard.main(['refresh',str(self.f.bank),str(self.f.output),str(self.html),'--offline-bank']),1)
        pull.assert_not_called()
