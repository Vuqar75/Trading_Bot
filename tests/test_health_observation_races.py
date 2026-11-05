import importlib.util
import json
import runpy
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_data_sync import CrossDataSync

spec=importlib.util.spec_from_file_location('health_verifier',Path(__file__).resolve().parents[1]/'tools/margin_verify_vm.py')
verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)


class HealthObservationRaceTests(unittest.TestCase):
    def test_health_published_after_observation_start_is_not_future(self):
        health={'time':100.2,'error':None,'blocked':None}
        self.assertFalse(0<=100.0-health['time']<=120)  # old pre-read time
        self.assertIsNone(verifier.health_record_problem(health,checked_at=100.3,started_at=90))
        self.assertIsNotNone(verifier.health_record_problem(health,checked_at=100.1,started_at=90))
        self.assertIsNotNone(verifier.health_record_problem(health,checked_at=221,started_at=90))
        self.assertIsNotNone(verifier.health_record_problem(dict(health,error='ValueError'),checked_at=100.3,started_at=90))

    def test_watchdog_does_not_pass_pre_collection_time_to_publish(self):
        from trading_bot import paper_watchdog_runtime as runtime
        script=Path(__file__).resolve().parents[1]/'deploy/paper_watchdog.py'
        with patch.object(runtime,'collect',return_value={'marker':'after collection'}),patch.object(runtime,'publish',return_value={'problems':0}) as pub,patch.object(sys,'argv',[str(script)]):
            runpy.run_path(str(script),run_name='__main__')
        self.assertNotIn('now',pub.call_args.kwargs)
        self.assertEqual(pub.call_args.args[1],{'marker':'after collection'})

    def test_refresh_marker_never_validates_failed_stale_or_other_process(self):
        marker={'phase':'refreshing','started_at':99,'observed_at':100,'pid':42}
        self.assertTrue(verifier.cross_refresh_in_progress(marker,checked_at=101,pid=42))
        for altered in (dict(marker,phase='failed'),dict(marker,pid=43),dict(marker,observed_at=102),dict(marker,started_at=102)):
            self.assertFalse(verifier.cross_refresh_in_progress(altered,checked_at=101,pid=42))
        self.assertFalse(verifier.cross_refresh_in_progress(marker,checked_at=221,pid=42))

    def test_sync_diagnostics_do_not_leak_exception_or_enable_incomplete_data(self):
        class Book:
            def __init__(self):
                self.shared=None;self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE cross_runtime(key TEXT,value TEXT)');self.invalid=False;self.paused=False
            def invalidate_all(self):self.invalid=True
            def _symbols(self):return ('BTCUSDT',)
            def fail_closed(self):self.invalid=True;self.paused=True
        class Reader:
            def schedule(self,symbol):raise OSError('token=secret https://example.invalid/?signature=secret')
        book=Book();self.addCleanup(book.db.close);events=[]
        with patch('trading_bot.cross_data_sync.CrossFundingTracker'):
            sync=CrossDataSync(book,Reader(),diagnostic=events.append)
        with self.assertRaises(OSError):sync.refresh(now=100)
        self.assertTrue(book.invalid);self.assertTrue(book.paused)
        self.assertEqual([e['phase'] for e in events],['refreshing','failed'])
        self.assertEqual(events[-1]['exception_type'],'OSError')
        self.assertNotIn('secret',json.dumps(events));self.assertNotIn('https',json.dumps(events))
