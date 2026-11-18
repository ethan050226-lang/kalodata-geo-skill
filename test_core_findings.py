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
        result = apply(self.html,self.file)
        self.assertEqual(result['status'],'analysis_ready')
        updated=dashboard.retained_data(self.html)[1]
        self.assertEqual(result['report'], report(updated))
        self.assertIn('SYNTHETIC overall',result['report']['conclusion'])
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

    def test_prose_numbers_cannot_disagree_with_validated_metrics(self):
        good=synthetic_findings(self.data)
        pair=good['entries'][0]['metrics']['mention']
        for text in ('提及率47.09%（999/1000）',
                     f'提及率{100*pair[0]/pair[1]+10:.2f}%（{pair[0]}/{pair[1]}）'):
            bad=deepcopy(good);bad['entries'][0]['conclusion']=text
            self.file.write_text(json.dumps(bad));before=self.html.read_bytes()
            with self.subTest(text=text),self.assertRaisesRegex(ArchiveError,'文案'):
                apply(self.html,self.file)
            self.assertEqual(self.html.read_bytes(),before)

    def test_formatted_claims_allow_rounding_and_do_not_parse_dates(self):
        from core_findings import validate_claims
        expected={'mention':[1000,3000],'first':[2000,4000],
                  'negative':[0,4000],'mixed':[1,4000]}
        validate_claims('2026/11/12 至 2026/11/18；提及率33.33%（1,000 / 3,000），首位率50% (2000/4000)',expected)
        with self.assertRaisesRegex(ArchiveError,'文案'):
            validate_claims('提及率33.34%（1,000/3,000）',expected)


    def test_named_numeric_claims_reject_wrong_metric_and_bare_percent(self):
        from core_findings import validate_claims
        expected={'mention':[80,100],'first':[20,100],'negative':[5,100],'mixed':[10,100]}
        bad = ('提及率为99%', '提及率20%（20/100）', '提及率99%，分子分母为80/100',
               '80%', '80/100', '提及率和首位率20%', '提及率80%。20%',
               '提及率80%，首位率80/100', '提及率8e1%', '提及率-80%',
               '提及率80%，预计增长20%', '提及率80/100/50',
               '提及率80.000000000000000000000000000000%')
        for text in bad:
            with self.subTest(text=text), self.assertRaisesRegex(ArchiveError,'文案'):
                validate_claims(text,expected)
        good = ('提及率80%', '提及率80/100', '提及率80%（80/100）',
                '提及率80/100（80%）', '提及率80%，分子分母为80/100',
                '提及率８０％（８０／１００）',
                '提及率80%，首位率20%（20/100）；负面率5%，褒贬并存率10%',
                'mention: 80% (80/100); first: 20% (20/100)',
                '2026/11/12 至 2026/11/18；提及率80%（80/100）')
        for text in good:
            with self.subTest(text=text):validate_claims(text,expected)

    def test_zero_denominator_and_rounding(self):
        from core_findings import validate_claims
        expected={'mention':[0,0],'first':[1,6],'negative':[0,6],'mixed':[0,6]}
        validate_claims('提及率不适用；首位率16.67%（1/6）；负面率0%',expected)
        for text in ('提及率0%', '首位率16.66%', '首位率16.68%'):
            with self.subTest(text=text), self.assertRaisesRegex(ArchiveError,'文案'):
                validate_claims(text,expected)

    def test_invalid_prose_in_either_field_preserves_html_and_stops_publish(self):
        from feishu_publish import publish
        good=synthetic_findings(self.data)
        for field in ('conclusion','judgment'):
            bad=deepcopy(good);bad['entries'][0][field]='提及率99.99999%'
            self.file.write_text(json.dumps(bad));before=self.html.read_bytes()
            with self.subTest(field=field),self.assertRaisesRegex(ArchiveError,'文案'):
                apply(self.html,self.file)
            self.assertEqual(before,self.html.read_bytes())
            data=deepcopy(self.data);data['core_findings']=bad
            remote=Mock()
            with patch.object(__import__('feishu_publish'),'retained_data',return_value=('',data,'')):
                with self.assertRaises(ArchiveError):
                    publish(self.html,self.target,{},remote=remote)
            self.assertEqual(remote.mock_calls,[])

    def test_default_country_combination_is_required_before_sharing(self):
        f=self.fixture
        book=country_fixture.load_workbook(f.f.bank)
        sheet=book.create_sheet('日本');sheet.append(['分类名称','编号','问题内容'])
        sheet.append(['01 品类发现','TEST-01-JP','Japanese seller question?'])
        book['国家模型配置'].append(['日本','chatgpt']);book.save(f.f.bank);book.close()
        extra=deepcopy(f.records[5]);extra.update(task_id='JP-chatgpt',query_str='Japanese seller question?',language='ja')
        f.records.append(extra);f.refresh()
        target=f.root/'three-countries.sqlite'
        restrict_snapshot(f.f.output,target,['CN','TH','JP'],model_policy=load_model_policy(f.f.bank))
        data=dashboard.dashboard_data(target,f.f.bank,None)
        data['presentation']={'default_markets':['CN','TH']}
        draft=context(data);default=next(s for s in draft['required_scopes'] if s['name']=='default')
        self.assertEqual({data['rows'][i]['d'][0] for i in default['row_indices']},{'CN','TH'})
        good=synthetic_findings(data);validate(data,good)
        good['entries']=[e for e in good['entries'] if e['row_indices']!=default['row_indices']]
        with self.assertRaisesRegex(ArchiveError,'默认组合'):
            validate(data,good)

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
