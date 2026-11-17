from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from archive import ArchiveError
from core_findings import context, validate, apply
import dashboard
from feishu_delivery import shared_data
from market_scope import load_model_policy, restrict_snapshot
from publication import report
import test_country_models as country_fixture


def synthetic_findings(data):
    draft=context(data)
    seen=set();entries=[]
    for scope in draft['required_scopes']:
        key=tuple(scope['row_indices'])
        if key in seen:continue
        seen.add(key)
        entries.append({'row_indices':scope['row_indices'],'metrics':scope['metrics'],
                        'conclusion':'SYNTHETIC '+scope['name']+' verified observation',
                        'judgment':'SYNTHETIC check before optimization'})
    return {'version':draft['version'],'data_sha256':draft['data_sha256'],'entries':entries}


class CoreFindingsTest(unittest.TestCase):
    def setUp(self):
        self.fixture=country_fixture.CountryModelTest();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        self.target=f.root/'two-countries.sqlite'
        restrict_snapshot(f.f.output,self.target,['CN','TH'],model_policy=load_model_policy(f.f.bank))
        self.data=dashboard.dashboard_data(self.target,f.f.bank,None)
        self.html=f.root/'findings.html';dashboard.render(self.data,self.html)
        self.file=f.root/'synthetic.findings.json'

    def test_agent_analysis_is_bound_to_same_batch_and_report(self):
        findings=synthetic_findings(self.data);self.file.write_text(json.dumps(findings))
        self.assertEqual(apply(self.html,self.file)['status'],'analysis_ready')
        updated=dashboard.retained_data(self.html)[1]
        self.assertIn('SYNTHETIC overall',report(updated)['conclusion'])
        shared=shared_data(updated)
        self.assertEqual(shared['core_findings'],findings)
        self.assertNotIn('PRIVATE_ORIGINAL',json.dumps(shared))
        self.assertEqual({r['d'][1] for r in shared['rows'] if r['d'][0]=='CN'},{'chatgpt','deepseek'})

    def test_stale_wrong_counts_and_wrong_scope_preserve_original(self):
        before=self.html.read_bytes();good=synthetic_findings(self.data)
        for change in ('hash','metrics','missing_country','invalid_index','secret','extra_field'):
            bad=deepcopy(good)
            if change=='hash':bad['data_sha256']='stale'
            elif change=='metrics':bad['entries'][0]['metrics']['mention'][0]+=1
            elif change=='missing_country':bad['entries'].pop()
            elif change=='invalid_index':bad['entries'][0]['row_indices']=[9999]
            elif change=='secret':bad['entries'][0]['conclusion']='sk-'+'x'*30
            else:bad['entries'][0]['body']='private'
            self.file.write_text(json.dumps(bad))
            with self.subTest(change=change),self.assertRaises(ArchiveError):apply(self.html,self.file)
            self.assertEqual(before,self.html.read_bytes())

    def test_generic_reminders_must_be_rewritten_as_specific_states(self):
        good=synthetic_findings(self.data)
        from core_findings import GENERIC_REMINDERS
        for phrase in GENERIC_REMINDERS:
            bad=deepcopy(good);bad['entries'][0]['judgment']=phrase
            with self.subTest(phrase=phrase),self.assertRaisesRegex(ArchiveError,'泛化提醒'):
                validate(self.data,bad)

    def test_data_change_invalidates_analysis_but_fetch_time_alone_does_not(self):
        good=synthetic_findings(self.data);changed=deepcopy(self.data)
        changed['batch']['fetched_at_utc']='2026-11-17T00:00:00Z'
        validate(changed,good)
        changed['rows'][0]['n']+=1
        with self.assertRaises(ArchiveError):validate(changed,good)
        changed=deepcopy(self.data);changed['question_scope']['answer_source_sha256']='changed-original'
        with self.assertRaises(ArchiveError):validate(changed,good)

    def test_unverified_legacy_or_illegal_country_model_cannot_be_analyzed(self):
        legacy=deepcopy(self.data);legacy.pop('model_policy')
        with self.assertRaisesRegex(ArchiveError,'未校验国家模型'):context(legacy)
        illegal=deepcopy(self.data);illegal['rows'][0]['d']=list(illegal['rows'][0]['d']);illegal['rows'][0]['d'][1]='perplexity'
        with self.assertRaisesRegex(ArchiveError,'范围外模型'):context(illegal)

    def test_missing_analysis_stops_upload_and_local_run_supplies_context(self):
        import geo_run
        from feishu_publish import publish
        remote=Mock()
        with self.assertRaisesRegex(ArchiveError,'缺少同批Agent分析'):
            publish(self.html,self.target,{},remote=remote)
        self.assertEqual(remote.mock_calls,[])
        with patch.object(dashboard,'main',return_value=0),patch.object(geo_run,'load_config') as config:
            result,code=geo_run.run(self.fixture.f.bank,self.target,self.html,markets=['CN','TH'],local_only=True)
        self.assertEqual((code,result['status']),(0,'local_ready_pending_analysis'))
        self.assertTrue(__import__('pathlib').Path(result['local']['analysis_context']).is_file())
        config.assert_not_called()


if __name__=='__main__':unittest.main()
