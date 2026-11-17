from copy import deepcopy
from io import BytesIO
from pathlib import Path
from contextlib import redirect_stdout,redirect_stderr
import io
import json
import sqlite3
import unittest
from unittest.mock import patch,Mock

from openpyxl import load_workbook
import answerbit
import dashboard
from archive import ArchiveError
from feishu_delivery import shared_data
from market_scope import load_model_policy,require_model_policy,restrict_snapshot,model_identifier,sampling_plan
from publication import manifest,report
from workbook_export import export_workbook
import test_answerbit as fixture

APPROVED=Path(__file__).parent/'skill/geo-sampling/references/country-models.json'

class CountryModelTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.AnswerbitTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.output.parent;book=load_workbook(self.f.bank)
        for country,code,text in [('中国','CN','中国团队分析美国市场用什么工具？'),('泰国','TH','Which tools help a Thai seller?')]:
            sheet=book.create_sheet(country);sheet.append(['分类名称','编号','问题内容'])
            sheet.append(['01 品类发现','TEST-01-'+code,text])
        config=book['国家模型配置']
        config.append(['中国','deepseek、chatgpt']);config.append(['泰国','chatgpt、gemini'])
        book.save(self.f.bank);book.close()
        self.records=[]
        for country,text,models,language in [('CN','中国团队分析美国市场用什么工具？',
                                              ['deepseek','chatgpt','gemini','perplexity','google_ai_mode'],'zh'),
                                             ('TH','Which tools help a Thai seller?',
                                              ['chatgpt','doubao','unknown_engine'],'th')]:
            for model in models:
                self.records.append(dict(task_id=country+'-'+model,query_str=text,platform=model,language=language,
                    date=self.f.day,zone='US',analysis={'state':2},title_id='group',title_name='发现',
                    tags=[{'tag_id':'region','tag_name':'欧美' if country=='CN' else '东南亚','tag_type':1}]))
        self.refresh()

    def call(self,path,payload):
        if path==answerbit.LIST:return {'scores':self.records,'total':len(self.records)}
        row=next(r for r in self.records if r['task_id']==payload['task_id'])
        return dict(prompt=row['query_str'],platform=row['platform'],language=row['language'],date=self.f.day,
                    links=[],brand_reputation=2 if row['task_id']=='CN-chatgpt' else 1,
                    llm_output='Kalodata. PRIVATE_ORIGINAL_'+row['task_id'])

    def refresh(self):
        dashboard.refresh_answers(self.f.bank,self.f.output,'synthetic-brand',self.f.day,self.f.day,self.call)

    def scope(self,country):
        target=self.root/(country+'.sqlite')
        restrict_snapshot(self.f.output,target,[country],model_policy=load_model_policy(self.f.bank))
        return target,dashboard.dashboard_data(target,self.f.bank,None)

    def test_cn_uses_bank_country_and_model_scope_across_all_outputs(self):
        source=self.f.output.read_bytes();target,data=self.scope('CN')
        self.assertEqual({r['d'][0] for r in data['rows']},{'CN'})
        self.assertEqual({r['d'][1] for r in data['rows']},{'deepseek','chatgpt'})
        self.assertEqual(data['core_mention'],{'numerator':1,'denominator':2,'excluded_negative':1,'value':0.5})
        self.assertEqual(data['batch']['returned_records'],2)
        self.assertEqual(len(data['model_policy']['excluded_records']),3)
        self.assertEqual(report(data)['model_scope']['excluded'],3)
        book=load_workbook(BytesIO(export_workbook(target,data)));rows=list(book['回答记录'].values)
        self.assertEqual(len(rows),3);self.assertEqual(book['国家模型排除'].max_row,4);book.close()
        dashboard.render(shared_data(data),self.root/'synthetic-country-model.html')
        self.assertEqual(manifest(target,data)['sample_count'],2)
        self.assertEqual(self.f.output.read_bytes(),source)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sample_answer').fetchone()[0],2)
        self.assertNotIn(b'PRIVATE_ORIGINAL_CN-gemini',target.read_bytes())
        self.assertNotIn('PRIVATE_ORIGINAL',json.dumps(shared_data(data)))

    def test_southeast_scope_excludes_unconfigured_and_unknown_models(self):
        target,data=self.scope('TH')
        self.assertEqual({r['d'][1] for r in data['rows']},{'chatgpt'})
        self.assertEqual(data['core_mention']['denominator'],1)
        reasons={r['platform']:r['reason'] for r in data['model_policy']['excluded_records']}
        self.assertEqual(reasons,{'doubao':'不在该国家已确认模型范围','unknown_engine':'模型未识别'})
        self.assertNotIn(b'PRIVATE_ORIGINAL_TH-doubao',target.read_bytes())

    def test_explicit_country_tag_conflict_is_not_overridden_by_text_or_zone(self):
        _,questions,by_text=answerbit.load_bank(self.f.bank);row=deepcopy(self.records[0])
        self.assertEqual(answerbit.select_question(row,questions,by_text),('TEST-01-CN',None))
        row['tags']=[{'tag_name':'美国'}]
        self.assertEqual(answerbit.select_question(row,questions,by_text),(None,'market_tag_conflict'))

    def test_missing_policy_stops_before_answerbit_and_keeps_previous_output(self):
        book=load_workbook(self.f.bank);book.remove(book['国家模型配置']);book.save(self.f.bank);book.close()
        policy=load_model_policy(self.f.bank)
        with self.assertRaisesRegex(ArchiveError,'未在题库找到'):require_model_policy(policy,['CN'])
        output=self.root/'previous.html';output.write_text('PREVIOUS_SUCCESS')
        call=Mock(side_effect=AssertionError('Answerbit must not be requested'))
        with (patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')),
             patch.object(answerbit,'question_bank_url',return_value='https://example.feishu.cn/wiki/synthetic'),
             patch('feishu_bank.fetch_bank',return_value={'source':'feishu-live','revision':1}),
             patch.object(dashboard,'PacedRead',return_value=call),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO())):
            self.assertEqual(dashboard.main(['refresh',str(self.f.bank),str(self.f.output),str(output),'--market','CN']),1)
        call.assert_not_called();self.assertEqual(output.read_text(),'PREVIOUS_SUCCESS')

    def test_policy_changes_prevent_history_delta_and_change_version(self):
        old_target,old=self.scope('CN');old_version=manifest(old_target,old)
        book=load_workbook(self.f.bank);book['国家模型配置'].append(['中国','qianwen']);book.save(self.f.bank);book.close()
        self.refresh();new_target,new=self.scope('CN')
        comparison=dashboard.previous_comparison(new,old,refresh=True)
        self.assertIn('国家模型配置或过滤口径不同',comparison['limitations'])
        self.assertTrue(all(r['change'] is None for r in comparison['metrics']))
        self.assertNotEqual(manifest(new_target,new)['version'],old_version['version'])

    def test_conflicting_policy_and_unreviewed_fields_stop_sharing(self):
        _,data=self.scope('CN');bad=deepcopy(data)
        bad['model_policy']['excluded_records'][0]['body']='PRIVATE'
        with self.assertRaises(ArchiveError):shared_data(bad)
        book=load_workbook(self.f.bank);book['国家模型配置'].append(['中国','Gemini']);book.save(self.f.bank);book.close()
        with self.assertRaisesRegex(ArchiveError,'要求冲突'):load_model_policy(self.f.bank)
        self.assertEqual(model_identifier('AI Mode'),'google_ai_mode')
        self.assertNotEqual(model_identifier('Google'),'google_ai_mode')

    def without_bank_model_table(self):
        book=load_workbook(self.f.bank);book.remove(book['国家模型配置']);book.save(self.f.bank);book.close()

    def approved(self, *, resolve=False):
        path=APPROVED
        if resolve:
            config=json.loads(path.read_text())
            for name,platform in [('Google AIO','synthetic-aio'),('Claude','synthetic-claude')]:
                config['platforms'][name]={'id':platform,'evidence':'SYNTHETIC mapping only'}
            path=self.root/'synthetic-approved.json';path.write_text(json.dumps(config))
        return load_model_policy(self.f.bank,path)

    def test_confirmed_country_table_includes_japan_korea_without_claude(self):
        self.without_bank_model_table();policy=self.approved()
        require_model_policy(policy,['CN'])
        self.assertEqual(policy['models']['CN'],sorted(['chatgpt','doubao','qianwen','deepseek','yuanbao','yiyan','kimi']))
        for market in ('US','GB','DE','FR'):
            require_model_policy(policy,[market])
            self.assertIn('google_ai_overviews',policy['models'][market])
        for market in ('TH','VN','ID','MY','JP','KR'):
            require_model_policy(policy,[market])
            self.assertNotIn(market,policy.get('pending',{}))
            self.assertNotIn('Claude',policy['requested_models'][market])
        self.assertEqual(set(policy['models']),set(answerbit.MARKETS.values()))
        for market in ('JP','KR'):
            self.assertEqual(set(policy['models'][market]),{'chatgpt','gemini','google_ai_mode','google_ai_overviews'})

    def test_future_unconfirmed_country_policy_still_blocks(self):
        self.without_bank_model_table();config=json.loads(APPROVED.read_text())
        config['countries']['JP'].update(status='incomplete',pending_reason='SYNTHETIC future unconfirmed range')
        path=self.root/'unconfirmed-country.json';path.write_text(json.dumps(config))
        policy=load_model_policy(self.f.bank,path)
        with self.assertRaisesRegex(ArchiveError,'future unconfirmed'):require_model_policy(policy,['JP'])
        require_model_policy(policy,['KR'])

    def test_cn_seven_models_recompute_every_output_with_same_denominator(self):
        self.without_bank_model_table()
        models=['chatgpt','doubao','qianwen','deepseek','yuanbao','yiyan','kimi','gemini','perplexity','google_ai_mode']
        self.records=[dict(self.records[0],task_id='CN-'+m,platform=m) for m in models];self.refresh()
        target=self.root/'approved-cn.sqlite';source=self.f.output.read_bytes()
        restrict_snapshot(self.f.output,target,['CN'],model_policy=self.approved())
        data=dashboard.dashboard_data(target,self.f.bank,None)
        self.assertEqual(data['core_mention'],{'numerator':6,'denominator':7,'excluded_negative':1,'value':6/7})
        self.assertEqual(sum(r['n'] for r in data['rows']),7)
        metric=next(m for m in report(data)['metrics'] if m['id']=='skill.core_mention_rate')
        self.assertEqual((metric['numerator'],metric['denominator']),(6,7))
        book=load_workbook(BytesIO(export_workbook(target,data)))
        self.assertEqual(book['回答记录'].max_row,8);self.assertEqual(book['国家模型排除'].max_row,4);book.close()
        self.assertEqual(manifest(target,data)['sample_count'],7)
        self.assertNotIn('pending',data['model_policy']);shared_data(data)
        self.assertEqual(source,self.f.output.read_bytes())

    def test_indonesia_doubao_contamination_excluded_from_denominator_and_excel(self):
        self.without_bank_model_table();book=load_workbook(self.f.bank)
        sheet=book.create_sheet('印尼');sheet.append(['分类名称','编号','问题内容'])
        sheet.append(['01 品类发现','TEST-01-ID','Indonesian tools?']);book.save(self.f.bank);book.close()
        self.records=[dict(self.records[-1],task_id='ID-'+m,query_str='Indonesian tools?',platform=m)
                      for m in ('chatgpt','gemini','synthetic-claude','doubao','unknown_engine')];self.refresh()
        target=self.root/'approved-id.sqlite'
        restrict_snapshot(self.f.output,target,['ID'],model_policy=self.approved(resolve=True))
        data=dashboard.dashboard_data(target,self.f.bank,None)
        self.assertEqual(data['core_mention']['denominator'],2)
        self.assertNotIn('doubao',{r['d'][1] for r in data['rows']})
        self.assertEqual({r['platform'] for r in data['model_policy']['excluded_records']},{'doubao','unknown_engine','synthetic-claude'})
        book=load_workbook(BytesIO(export_workbook(target,data)))
        self.assertEqual(book['回答记录'].max_row,3);self.assertEqual(book['国家模型排除'].max_row,4);book.close()
        self.assertEqual(manifest(target,data)['sample_count'],2)
        self.assertEqual(report(data)['model_scope']['excluded'],3)
        shared_data(data)

    def test_europe_five_platforms_stay_distinct_with_verified_aio_mapping(self):
        self.without_bank_model_table();book=load_workbook(self.f.bank)
        book['美国']['C2']='European tools?';book.save(self.f.bank);book.close()
        models=('chatgpt','gemini','google_ai_overviews','perplexity','google_ai_mode','google','deepseek')
        self.records=[dict(self.records[0],task_id='US-'+m,query_str='European tools?',platform=m) for m in models];self.refresh()
        target=self.root/'approved-us.sqlite'
        restrict_snapshot(self.f.output,target,['US'],model_policy=self.approved())
        data=dashboard.dashboard_data(target,self.f.bank,None)
        self.assertEqual({r['d'][1] for r in data['rows']},set(models[:5]))
        self.assertEqual(data['core_mention']['denominator'],5)
        self.assertEqual(model_identifier('GPT'),'chatgpt')
        self.assertNotEqual(model_identifier('google'),'google_ai_mode')

    def test_unresolved_aio_stops_before_real_reads_and_preserves_success(self):
        self.without_bank_model_table();output=self.root/'previous-approved.html';output.write_text('PREVIOUS_SUCCESS')
        call=Mock(side_effect=AssertionError('No Answerbit call before policy passes'))
        errors=[]
        config=json.loads(APPROVED.read_text());config['platforms']['Google AIO']['id']=None
        unresolved=self.root/'unresolved-aio.json';unresolved.write_text(json.dumps(config))
        with (patch('market_scope.APPROVED_MODEL_POLICY',unresolved),
              patch.object(answerbit,'operator_credentials',return_value=('TEST_ONLY','synthetic')),
              patch.object(answerbit,'question_bank_url',return_value='https://example.feishu.cn/wiki/synthetic'),
              patch('feishu_bank.fetch_bank',return_value={'source':'feishu-live','revision':1}),
              patch.object(dashboard,'PacedRead',return_value=call),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO())):
            self.assertEqual(dashboard.main(['refresh',str(self.f.bank),str(self.f.output),str(output),'--market','US'],status_callback=errors.append),1)
        call.assert_not_called();self.assertEqual(output.read_text(),'PREVIOUS_SUCCESS')
        self.assertEqual(errors[0]['stage'],'国家模型配置核对')
        self.assertIn('Google AIO',errors[0]['error'])

    def test_bank_conflict_and_merged_platform_ids_fail_closed(self):
        with self.assertRaisesRegex(ArchiveError,'题库与用户已确认配置冲突'):self.approved()
        self.without_bank_model_table();config=json.loads(APPROVED.read_text())
        config['platforms']['Google AIO']={'id':'google_ai_mode','evidence':'bad merge'}
        path=self.root/'bad-merged.json';path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ArchiveError,'不能合并'):load_model_policy(self.f.bank,path)

    def test_country_column_conflict_and_out_of_scope_references_are_rejected(self):
        _,data=self.scope('CN');bad=deepcopy(data)
        bad['recommendations'][0]['d']=list(bad['recommendations'][0]['d'])
        bad['recommendations'][0]['d'][1]='gemini'
        with self.assertRaisesRegex(ArchiveError,'题单包含范围外模型'):shared_data(bad)
        self.without_bank_model_table();book=load_workbook(self.f.bank)
        sheet=book['中国'];sheet['D1']='采样模型';sheet['D2']='chatgpt、deepseek'
        book.save(self.f.bank);book.close()
        self.assertEqual(load_model_policy(self.f.bank)['models']['CN'],['chatgpt','deepseek'])
        book=load_workbook(self.f.bank);book['中国'].append(['测试','TEST-02-CN','Different question?','chatgpt'])
        book.save(self.f.bank);book.close()
        with self.assertRaisesRegex(ArchiveError,'同国家行配置不一致'):load_model_policy(self.f.bank)

    def test_six_countries_plan_and_existing_answers_include_both_google_modes(self):
        self.without_bank_model_table();book=load_workbook(self.f.bank)
        markets=['TH','VN','ID','MY','JP','KR'];texts={}
        for country,code in answerbit.MARKETS.items():
            if code not in markets:continue
            sheet=book[country] if country in book.sheetnames else book.create_sheet(country)
            if country not in ('泰国',):sheet.append(['分类名称','编号','问题内容'])
            text='Which tools for '+code+'?';texts[code]=text
            sheet.append(['01 品类发现','TEST-GOOGLE-'+code,text])
        book.save(self.f.bank);book.close()
        actual=self.approved();plan=sampling_plan(actual,markets)
        self.assertFalse(plan['creates_tasks']);self.assertFalse(plan['platform_sampling_verified'])
        for code in markets:
            self.assertEqual(plan['countries'][code]['Google AI Mode'],'google_ai_mode')
            self.assertEqual(plan['countries'][code]['Google AIO'],'google_ai_overviews')
            self.assertEqual(plan['countries'][code]['Gemini'],'gemini')
            self.assertNotEqual(plan['countries'][code]['Google AIO'],plan['countries'][code]['Google AI Mode'])
        policy=self.approved();require_model_policy(policy,markets)
        allowed=['chatgpt','gemini','google_ai_mode','google_ai_overviews']
        self.records=[dict(self.records[0],task_id=code+'-'+m,query_str=texts[code],platform=m)
                      for code in markets for m in allowed+['synthetic-claude','doubao','unknown_engine']];self.refresh()
        source=self.f.output.read_bytes()
        for code in markets:
            with self.subTest(country=code):
                target=self.root/('six-'+code+'.sqlite')
                restrict_snapshot(self.f.output,target,[code],model_policy=policy)
                data=dashboard.dashboard_data(target,self.f.bank,None)
                self.assertEqual({r['d'][1] for r in data['rows']},set(allowed))
                self.assertEqual(data['core_mention']['denominator'],4)
                self.assertEqual(report(data)['sampling_requirements']['countries'][code]['Google AI Mode'],'google_ai_mode')
                book=load_workbook(BytesIO(export_workbook(target,data)))
                self.assertEqual(book['采样要求'].max_row,5)
                self.assertTrue(all(r[3]==1 for r in list(book['采样要求'].values)[1:]));book.close()
                self.assertEqual(manifest(target,data)['sample_count'],4);shared_data(data)
        self.assertEqual(source,self.f.output.read_bytes())

    def test_deferred_claude_needs_no_id_but_unresolved_aio_still_blocks(self):
        self.without_bank_model_table();config=json.loads(APPROVED.read_text())
        self.assertIsNone(config['platforms']['Claude']['id'])
        markets=['TH','VN','ID','MY','JP','KR']
        config['platforms']['Google AIO']['id']=None
        unknown=self.root/'missing-aio-id.json';unknown.write_text(json.dumps(config))
        policy=load_model_policy(self.f.bank,unknown);plan=sampling_plan(policy,markets)
        self.assertIn('Claude',plan['deferred_models'])
        for code in markets:
            self.assertNotIn('Claude',plan['countries'][code])
            self.assertIn('Google AIO',str(plan['pending'][code]))
            self.assertNotIn('Claude 的 Answerbit',str(plan['pending'][code]))
            with self.assertRaisesRegex(ArchiveError,'Google AIO'):require_model_policy(policy,[code])
        config['platforms']['Google AIO']={'id':'synthetic-aio','evidence':'SYNTHETIC mapping only'}
        path=self.root/'deferred-claude.json';path.write_text(json.dumps(config))
        policy=load_model_policy(self.f.bank,path);require_model_policy(policy,markets)
        for code in markets:
            self.assertEqual(set(policy['models'][code]),{'chatgpt','gemini','google_ai_mode','synthetic-aio'})
