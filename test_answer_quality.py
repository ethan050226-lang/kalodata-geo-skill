from copy import deepcopy
from io import BytesIO
import sqlite3
import unittest
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

from openpyxl import load_workbook
import answer_quality as quality
import answerbit
import dashboard
from feishu_delivery import shared_data
from market_scope import restrict_snapshot
from local_answers import make_server
from archive import ArchiveError
import test_answerbit as fixtures
from workbook_export import export_workbook


class QualityTest(unittest.TestCase):
    def row(self, task, body, key=('same-question', 'perplexity', 'en', 'US')):
        return dict(task=task, prompt='Which tool should I use?', body=body, key=key)

    def test_errors_echo_truncation_and_refusal_not_ordinary_content(self):
        examples = [None, '', 'Request failed: HTTP 503', '抱歉，我无法回答这个问题。',
                    'Which tool should I use?', 'Some content [response truncated]']
        for body in examples:
            self.assertEqual(quality.assess([self.row('x', body)])['x']['status'], 'invalid')
        for body in ('Kalodata.', 'Use Kalodata for product research.',
                     'Error handling matters for a reliable workflow.',
                     '请求失败时请检查网络。',
                     'This guide explains how to fix a request failed error.',
                     'I cannot provide exact sales. Use Kalodata estimates for research.',
                     "I cannot provide exact sales.\n\nUse estimated GMV as a directional reference."):
            self.assertEqual(quality.assess([self.row('x', body)])['x']['status'], 'accepted')

    def test_shrink_uses_peer_baseline_and_never_other_question_or_platform(self):
        long = 'Kalodata supports product research and competitor analysis. ' * 40
        short = 'Try FastMoss or EchoTik. Start with a low budget.'
        data = quality.assess([self.row('long', long), self.row('short', short)])
        self.assertEqual(data['short']['status'], 'accepted')
        self.assertIn('仍纳入统计',data['short']['reason'])
        self.assertEqual(data['short']['reference_task'], 'long')
        self.assertEqual(data['long']['status'], 'accepted')
        other = self.row('other', short, ('other-question', 'perplexity', 'en', 'US'))
        self.assertEqual(quality.assess([self.row('long', long), other])['other']['status'], 'accepted')
        self.assertEqual(quality.assess([self.row('only', short)])['only']['status'], 'accepted')
        for key in (('same-question','chatgpt','en','US'),('same-question','perplexity','en','GB')):
            other=self.row('other',short,key)
            self.assertEqual(quality.assess([self.row('long',long),other])['other']['status'],'accepted')
        typical = quality.assess([self.row('long', long), self.row('a', short), self.row('b', short)])
        self.assertEqual(typical['a']['status'], 'accepted')  # One long outlier is not a stable majority.

    def test_excluded_answers_keep_numbers_originals_and_leave_all_metric_denominators(self):
        fixture = fixtures.AnswerbitTest(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        template = fixture.records()[0]
        bodies = {'good':'Kalodata supports product research and competitor analysis. ' * 40,
                  'small':'Some output [response truncated]',
                  'error':'Request failed: HTTP 503', 'empty':''}
        records = [dict(deepcopy(template), task_id=task) for task in bodies]
        def call(path, payload):
            if path == answerbit.LIST: return {'scores':records, 'total':len(records)}
            return dict(prompt=template['query_str'], platform=template['platform'], language=template['language'],
                        date=fixture.day, llm_output=bodies[payload['task_id']], links=[], brand_reputation=1)
        dashboard.refresh_answers(fixture.bank,fixture.output,'synthetic',fixture.day,fixture.day,call)
        data = dashboard.dashboard_data(fixture.output,fixture.bank,None)
        self.assertEqual(sum(r['n'] for r in data['rows']),1)
        self.assertEqual(data['core_mention']['denominator'],1)
        self.assertEqual(data['core_mention']['numerator'],1)
        self.assertEqual({r['task'] for r in data['quality']['records']},{'small','error','empty'})
        self.assertTrue(all(r['q']=='TEST-01-GB' for r in data['quality']['records']))
        shared = shared_data(data)
        self.assertNotIn(bodies['good'],str(shared))
        self.assertNotIn(bodies['small'],str(shared))
        with sqlite3.connect(fixture.output) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sample_answer').fetchone()[0],4)
            self.assertEqual(db.execute("SELECT count(*) FROM sample WHERE status='data_anomaly'").fetchone()[0],3)
            self.assertEqual(db.execute('SELECT count(*) FROM sample_content').fetchone()[0],1)
        book=load_workbook(BytesIO(export_workbook(fixture.output,data)))
        self.addCleanup(book.close)
        self.assertEqual(book['回答质量'].max_row,5)
        rows=list(book['回答记录'].values)
        self.assertEqual(sum(r[7]=='数据异常' for r in rows[1:]),3)
        scoped=fixture.output.with_name('scoped.sqlite')
        restrict_snapshot(fixture.output,scoped,['GB'])
        scoped_data=dashboard.dashboard_data(scoped,fixture.bank,None)
        self.assertEqual(len(scoped_data['quality']['records']),3)
        html=fixture.output.with_suffix('.html'); dashboard.render(data,html)
        server,base=make_server(fixture.output,html)
        with server:
            worker=Thread(target=server.serve_forever,daemon=True); worker.start()
            try:
                for task in ('good','small','error','empty'):
                    with urlopen(base+'answer?task='+task,timeout=5) as reply:
                        self.assertIn('TEST-01-GB',reply.read().decode())
                with self.assertRaises(HTTPError): urlopen(base+'answer?task=unlisted',timeout=5)
            finally:
                server.shutdown(); worker.join()

    def test_all_invalid_keeps_previous_snapshot(self):
        fixture=fixtures.AnswerbitTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        row=fixture.records()[0]
        fixture.output.write_bytes(b'previous accepted snapshot')
        def call(path,payload):
            if path==answerbit.LIST: return {'scores':[row],'total':1}
            return dict(prompt=row['query_str'],platform=row['platform'],language=row['language'],date=fixture.day,
                        llm_output='Request failed: HTTP 503',links=[])
        with self.assertRaisesRegex(ArchiveError,'没有可判定的有效回答'):
            answerbit.refresh(fixture.bank,fixture.output,'synthetic',fixture.day,fixture.day,call,store_answers=True)
        self.assertEqual(fixture.output.read_bytes(),b'previous accepted snapshot')

    def test_cjk_shrink_is_flagged_without_dropping_short_standalone_answer(self):
        long='可以通过数据研究商品，比较竞品，寻找适合的达人。'*25
        short='可先从免费工具了解市场，再比较付费方案。'
        result=quality.assess([self.row('a',long),self.row('b',short)])
        self.assertEqual(result['b']['status'],'accepted')
        self.assertIn('仍纳入统计',result['b']['reason'])
        self.assertEqual(quality.assess([self.row('b',short)])['b']['status'],'accepted')

    def test_multilingual_pure_refusals_but_not_useful_followup(self):
        refusals = ['申し訳ありませんが、この質問には回答できません。',
                    '죄송하지만 이 질문에 답변할 수 없습니다.',
                    'Leider kann ich diese Anfrage nicht beantworten.',
                    'Désolé, je ne peux pas répondre à cette demande.',
                    'Xin lỗi, tôi không thể trả lời câu hỏi này.',
                    'ขออภัย ฉันไม่สามารถตอบคำถามนี้ได้',
                    'Maaf, saya tidak dapat menjawab pertanyaan ini.',
                    'Maaf, saya tidak dapat menjawab soalan ini.']
        for body in refusals:
            with self.subTest(body=body):
                self.assertEqual(quality.assess([self.row('x',body)])['x']['status'],'invalid')
                self.assertEqual(quality.assess([self.row('x',body+'\nUse Kalodata estimates for research.')])['x']['status'],'accepted')

    def test_error_envelopes_not_json_data_or_error_discussion(self):
        for body in ('{"error":"upstream failed","message":"try again"}',
                     '```json\n{"error":{"message":"timeout"},"data":null}\n```',
                     '糟糕，出了点问题，请稍后重试。', 'HTTP 503 Service unavailable'):
            with self.subTest(body=body):
                self.assertEqual(quality.assess([self.row('x',body)])['x']['status'],'invalid')
        for body in ('{"error":"timeout","analysis":"Kalodata supports product research"}',
                     '{"error":null,"data":[{"product":"Kalodata"}]}',
                     'HTTP 503 means the service is temporarily unavailable. Use cached research instead.',
                     'Request failed: HTTP 503. Kalodata supports product research.'):
            with self.subTest(body=body):
                self.assertEqual(quality.assess([self.row('x',body)])['x']['status'],'accepted')

    def test_short_incomplete_only_excluded_with_missing_requested_evidence(self):
        prompt='Explain the limitations of the tools and provide source links.'
        long=('Kalodata estimates require verification; see https://example.org/docs. ')*45
        incomplete='Kalodata and FastMoss are analytics tools.'
        concise='Kalodata uses estimates, not settlement data. Source: https://example.org/docs.'
        rows=[dict(self.row('long',long),prompt=prompt),dict(self.row('short',incomplete),prompt=prompt)]
        result=quality.assess(rows)
        self.assertEqual(result['short']['status'],'shrunken')
        self.assertIn('来源链接',result['short']['reason'])
        self.assertIn('限制说明',result['short']['reason'])
        result=quality.assess(rows+[dict(self.row('concise',concise),prompt=prompt)])
        self.assertEqual(result['short']['status'],'shrunken')
        self.assertEqual(result['concise']['status'],'accepted')
        self.assertIn('仍纳入统计',result['concise']['reason'])
        # Most peers being short must not erase explicit missing-request evidence.
        result=quality.assess(rows+[dict(rows[1],task='short2')])
        self.assertEqual(result['short2']['status'],'shrunken')
        # A long peer that also omits the requested parts is not evidence of completeness.
        rows[0]['body']='Kalodata supports product research. '*100
        self.assertEqual(quality.assess(rows)['short']['status'],'accepted')

    def test_complete_concise_answer_and_unrelated_reference_are_not_excluded(self):
        prompt='说明工具的局限，并提供来源链接。'
        long='数据为估算值，需核实。来源：https://example.org/docs。'*80
        short='Kalodata 是估算数据，仅作参考：https://example.org/docs。'
        rows=[dict(self.row('a',long),prompt=prompt),dict(self.row('b',short),prompt=prompt)]
        self.assertEqual(quality.assess(rows)['b']['status'],'accepted')
        rows[1]['body']='可用 Kalodata。'
        self.assertEqual(quality.assess(rows)['b']['status'],'shrunken')
        rows[1]['key']=('other-question','perplexity','zh','CN')
        self.assertEqual(quality.assess(rows)['b']['status'],'accepted')

    def test_negated_requests_are_not_omission_evidence(self):
        long='Kalodata estimates require verification. https://example.org/docs. '*70
        for prompt in ('Explain limitations; do not provide source links.',
                       "Explain limitations; don't provide source links.",
                       '说明限制；不要提供来源链接。'):
            with self.subTest(prompt=prompt):
                rows=[dict(self.row('long',long),prompt=prompt),
                      dict(self.row('short','Kalodata is a research tool.'),prompt=prompt)]
                self.assertEqual(quality.assess(rows)['short']['status'],'accepted')

    def test_length_warning_keeps_metrics_and_actual_historical_rule_version(self):
        fixture=fixtures.AnswerbitTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        template=fixture.records()[0]
        bodies={'long':'Kalodata supports product research. '*70,'short':'Use Kalodata for research.'}
        def call(path,payload):
            if path==answerbit.LIST:return {'scores':[dict(template,task_id=t) for t in bodies],'total':len(bodies)}
            return dict(prompt=template['query_str'],platform=template['platform'],language=template['language'],
                        date=fixture.day,llm_output=bodies[payload['task_id']],links=[],brand_reputation=1)
        dashboard.refresh_answers(fixture.bank,fixture.output,'synthetic',fixture.day,fixture.day,call)
        current=dashboard.dashboard_data(fixture.output,fixture.bank,None)
        self.assertEqual(current['core_mention']['denominator'],2)
        self.assertEqual(current['core_mention']['numerator'],2)
        self.assertEqual([(r['task'],r['status']) for r in current['quality']['records']],[('short','accepted')])
        self.assertEqual(shared_data(current)['quality'],current['quality'])
        book=load_workbook(BytesIO(export_workbook(fixture.output,current)));self.addCleanup(book.close)
        self.assertIn('纳入统计（长度待核查）',[r[6] for r in list(book['回答质量'].values)[1:]])
        with sqlite3.connect(fixture.output) as db:
            db.execute("UPDATE sample_quality SET version='answer-quality/1'")
            db.execute("UPDATE batch SET metric_version='answerbit-observed/4-quality'")
        old=dashboard.dashboard_data(fixture.output,fixture.bank,None)
        self.assertEqual(old['quality']['version'],'answer-quality/1')
        self.assertEqual(old['analysis_version'],'geo-core/3-answer-quality')
        comparison=dashboard.previous_comparison(current,old,refresh=True)
        self.assertTrue(all(r['change'] is None for r in comparison['metrics']))
        self.assertIn('分析口径不同或上次未记录口径',comparison['limitations'])


if __name__ == '__main__': unittest.main()
