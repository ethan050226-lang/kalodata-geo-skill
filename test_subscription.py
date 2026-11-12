import json
from pathlib import Path
import tempfile
import unittest

from archive import ArchiveError
from subscription import configure


class SubscriptionTest(unittest.TestCase):
    def test_confirmation_reuse_custom_time_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.subscription.json'
            params=dict(owner='synthetic-owner',markets=['美国'],destination='synthetic-private-route')
            with self.assertRaises(ArchiveError): configure(path,**params)
            self.assertFalse(path.exists())
            first=configure(path,**params,confirmed=True)
            self.assertEqual((first['weekdays'],first['local_time'],first['status']),(['MO'],'11:30','waiting_for_validation'))
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            first['scheduler_job_id']='synthetic-native-job';path.write_text(json.dumps(first))
            second=configure(path,**params,confirmed=True,weekdays=['WE'],local_time='15:00')
            self.assertEqual(second['subscription_id'],first['subscription_id'])
            self.assertEqual(second['scheduler_job_id'],'synthetic-native-job')
            self.assertEqual((second['weekdays'],second['local_time']),(['WE'],'15:00'))
            before=path.read_bytes()
            for changes in ({'owner':'other-owner'},{'local_time':'25:00'},{'timezone':'not/a/zone'},{'markets':['XX']}):
                with self.assertRaises(ArchiveError): configure(path,**(params|changes),confirmed=True)
                self.assertEqual(path.read_bytes(),before)

    def test_daily_and_unchanged_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'draft.json'
            params=dict(owner='synthetic-owner',markets=['GB'],destination='synthetic-route',confirmed=True,frequency='daily')
            first=configure(path,**params)
            self.assertEqual(len(first['weekdays']),7)
            first['status']='paused';path.write_text(json.dumps(first))
            self.assertEqual(configure(path,**params)['status'],'paused')
