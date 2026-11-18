from copy import deepcopy
import unittest
from dashboard import ANALYSIS_VERSION, previous_comparison

class HistoryMetricsTest(unittest.TestCase):
    def test_new_cards_require_comparable_scope_and_nonzero_denominators(self):
        old={'batch':{'source':'Answerbit','question_version':'bank','metric_version':'v3','begin_date':'2026-11-01','end_date':'2026-11-07','fetched_at_utc':'2026-11-08T00:00:00Z'},
             'analysis_version':ANALYSIS_VERSION,'question_scope':{'verified':True,'signature':'same'},
             'rows':[{'n':4,'d':['US','test','2026-11-01','g',0,[]],'mentions':{'Kalodata':2},'negative_mentions':1,'first':1,'sent':{'negative':1,'mixed':1},'links':{}}]}
        current=deepcopy(old);current['batch'].update(begin_date='2026-11-08',end_date='2026-11-14');current['rows'][0]['first']=2
        result=previous_comparison(current,old);by_id={r['id']:r for r in result['metrics']}
        self.assertEqual(by_id['skill.core_mention_rate']['value'],25)
        self.assertEqual(by_id['skill.first_rate']['change'],25)
        self.assertEqual(by_id['skill.adverse_rate']['value'],50)
        current['question_scope']['signature']='different'
        self.assertTrue(all(r['change'] is None for r in previous_comparison(current,old)['metrics']))
        current['rows']=[]
        zero={r['id']:r for r in previous_comparison(current,old)['metrics']}
        self.assertIsNone(zero['skill.first_rate']['value']);self.assertIsNone(zero['skill.adverse_rate']['value'])

    def test_historical_formula_is_explicit_and_unknown_formula_stays_unknown(self):
        from dashboard import report_metrics, data_warnings
        data={'analysis_version':'geo-core/2-unnamed-negative',
              'rows':[{'n':4,'d':['US','test','2026-11-01','g',0,[]],
                       'mentions':{'Kalodata':3},'negative_mentions':1,'first':1,'sent':{},'links':{}}]}
        core=next(m for m in report_metrics(data) if m['id']=='skill.core_mention_rate')
        self.assertEqual((core['value'],core['numerator'],core['denominator']),(50,2,4))
        self.assertIn('原文质量待检查',data_warnings(data))
        data['analysis_version']='unknown'
        core=next(m for m in report_metrics(data) if m['id']=='skill.core_mention_rate')
        self.assertIsNone(core['value'])
