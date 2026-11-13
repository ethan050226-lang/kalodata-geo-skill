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
        self.assertEqual(data['short']['status'], 'shrunken')
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
                  'small':'Try FastMoss or EchoTik. Start with a low budget.',
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
        self.assertEqual(result['b']['status'],'shrunken')
        self.assertEqual(quality.assess([self.row('b',short)])['b']['status'],'accepted')


if __name__ == '__main__': unittest.main()
