from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from archive import ArchiveError
from dashboard import render, retained_data
from feishu_delivery import shared_data, prepare


class SharedDashboardTest(unittest.TestCase):
    def setUp(self):
        from dashboard import ANALYSIS_VERSION
        from answer_quality import VERSION
        self.data = {'batch': {'source': 'synthetic'}, 'rows': [{'n': 2, 'evidence': {
            'negative': {'q': 'Demo-01', 'text': 'Synthetic question', 'task': 'demo'}}}],
            'analysis_version':ANALYSIS_VERSION,
            'quality':{'version':VERSION,'checked':2,'comparable':0,'records':[]},
            'question_scope':{'answer_source_sha256':'synthetic-source'}}


    def test_only_locating_information_leaves_local_environment(self):
        shared = shared_data(self.data)
        self.assertEqual(shared['rows'], self.data['rows'])
        self.assertEqual(shared['delivery']['originals'], 'answerbit-only')
        self.assertNotIn('delivery', self.data)
        for key, value in [('body', 'raw text'), ('api_key', 'secret'),
                           ('text', 'sk-' + 'x'*24), ('text', 'http://localhost:1234/raw')]:
            bad = deepcopy(self.data)
            bad['rows'][0]['evidence']['negative'][key] = value
            with self.assertRaises(ArchiveError): shared_data(bad)
        bad = deepcopy(self.data); bad['unexpected_export'] = []
        with self.assertRaises(ArchiveError): shared_data(bad)

    def test_failed_refresh_keeps_last_shared_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp)/'local.html', Path(tmp)/'shared.html'
            render(self.data, source)
            prepare(source, target)
            self.assertEqual(retained_data(target)[1]['rows'], self.data['rows'])
            previous = target.read_bytes()
            source.with_suffix('.status.json').write_text('{}')
            with self.assertRaises(ArchiveError): prepare(source, target)
            self.assertEqual(target.read_bytes(), previous)
            with self.assertRaises(ArchiveError): prepare(source, source)

    def test_incomplete_historical_batch_cannot_be_prepared_for_sharing(self):
        for field in ('quality','question_scope'):
            bad=deepcopy(self.data);bad.pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(ArchiveError,'未校验'):
                shared_data(bad)
        bad=deepcopy(self.data);bad['analysis_version']='geo-core/2-unnamed-negative'
        with self.assertRaisesRegex(ArchiveError,'历史口径'):
            shared_data(bad)

    def test_partial_quality_coverage_cannot_be_shared(self):
        bad=deepcopy(self.data);bad['quality']['checked']=1
        with self.assertRaisesRegex(ArchiveError,'未校验'):shared_data(bad)
