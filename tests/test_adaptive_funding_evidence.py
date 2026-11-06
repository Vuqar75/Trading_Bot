import sqlite3,unittest
from research.adaptive_funding_evidence import record,validate_mark,match_observed_due

class FundingEvidenceTest(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:',isolation_level=None);self.addCleanup(self.db.close)
  self.row={'symbol':'BTCUSDT','fundingTime':28800001,'fundingRate':'0.0001','markPrice':'100'}
 def test_repeat_preserves_first_observation(self):
  self.assertFalse(record(self.db,self.row,observed_ms=28801000)['replayed'])
  got=record(self.db,self.row,observed_ms=28802000)
  self.assertTrue(got['replayed']);self.assertEqual(got['first_observed_ms'],28801000)
 def test_changed_event_is_durable_and_does_not_replace_history(self):
  record(self.db,self.row,observed_ms=28801000)
  with self.assertRaisesRegex(ValueError,'changed_requires_review'):record(self.db,{**self.row,'fundingRate':'0.0002'},observed_ms=28802000)
  self.assertEqual(self.db.execute('SELECT count(*) FROM funding_conflicts').fetchone()[0],1)
  self.assertIn('0.0001',self.db.execute('SELECT payload FROM funding_evidence').fetchone()[0])
 def test_wrong_symbol_future_or_nonfinite_rejected(self):
  for change in ({'symbol':'ETHUSDT'},{'fundingTime':28803000},{'markPrice':'NaN'},{'fundingRate':'Infinity'}):
   with self.assertRaises(ValueError):record(self.db,{**self.row,**change},observed_ms=28802000)
 def test_due_requires_actual_event_and_preserves_millisecond_offset(self):
  got=match_observed_due([self.row],due_ms=28800000,received_ms=28801000)
  self.assertEqual(got['time'],28800001);self.assertIsNone(got['publication_ms'])
  for rows in ([],[self.row,self.row]):
   with self.assertRaisesRegex(ValueError,'missing_or_ambiguous'):match_observed_due(rows,due_ms=28800000,received_ms=28801000)
 def test_futures_mark_requires_source_event_freshness(self):
  mark={'symbol':'BTCUSDT','time':1000,'nextFundingTime':2000,'markPrice':'100'}
  self.assertEqual(validate_mark(mark,received_ms=1001)['mark'],'100')
  for changed,received in ((dict(mark,time=1002),1001),(dict(mark,symbol='ETHUSDT'),1001),(mark,6001)):
   with self.assertRaises(ValueError):validate_mark(changed,received_ms=received)
