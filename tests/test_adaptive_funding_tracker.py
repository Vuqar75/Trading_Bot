import unittest
from decimal import Decimal
from research.adaptive_funding_tracker import register,advance,covered,audit
from research.adaptive_funding_evidence import record
from tests import test_adaptive_atomic_futures as fixture

class FundingTrackerTest(unittest.TestCase):
 def setUp(self):
  self.f=fixture.AtomicFuturesTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.assertEqual(self.f.call()['status'],'filled');self.pos=self.f.store.get('position');self.due=self.f.now+10000
  register(self.f.store,position_id=self.pos['id'],next_due_ms=self.due,observed_ms=self.f.now)
 def mark(self,now,due):return {'symbol':'BTCUSDT','time':now,'markPrice':'100','nextFundingTime':due}
 def history(self,now,rows):return {'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':self.due-1,'end_ms':now,'received_ms':now,'rows':rows}
 def test_coverage_and_actual_due_event_are_idempotent(self):
  f=self.f;now=f.now+1000
  self.assertEqual(advance(f.store,position_id=self.pos['id'],mark_response=self.mark(now,self.due),received_ms=now)['status'],'covered')
  now=self.due+10;row={'symbol':'BTCUSDT','fundingTime':self.due+1,'fundingRate':'0.001','markPrice':'100'};next_due=self.due+28800000
  result=advance(f.store,position_id=self.pos['id'],mark_response=self.mark(now,next_due),received_ms=now,history=self.history(now,[row]))
  self.assertEqual(result['status'],'applied');value=Decimal(f.store.get('position')['funding']);self.assertGreater(value,0)
  self.assertEqual(advance(f.store,position_id=self.pos['id'],mark_response=self.mark(now,next_due),received_ms=now)['status'],'covered')
  self.assertEqual(Decimal(f.store.get('position')['funding']),value);self.assertEqual(f.store.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],1)
 def test_missing_and_ambiguous_history_never_advances_cursor(self):
  f=self.f;now=self.due+10;old=f.store.get('position')['funding_cursor'];mark=self.mark(now,self.due+28800000)
  for history in (None,self.history(now,[]),self.history(now,[{'symbol':'BTCUSDT','fundingTime':self.due+1}]*2)):
   with self.assertRaises(ValueError):advance(f.store,position_id=self.pos['id'],mark_response=mark,received_ms=now,history=history)
  self.assertEqual(f.store.get('position')['funding_cursor'],old)
 def test_short_coverage_handoff_cannot_cross_due_or_expire(self):
  f=self.f;now=f.now+1000
  advance(f.store,position_id=self.pos['id'],mark_response=self.mark(now,self.due),received_ms=now)
  p=f.store.get('position')
  self.assertTrue(covered(f.store,p,now+100))
  self.assertFalse(covered(f.store,p,now+5001))
  self.assertFalse(covered(f.store,p,self.due))
 def test_crash_after_financial_apply_replays_without_double_funding(self):
  f=self.f;now=self.due+10;row={'symbol':'BTCUSDT','fundingTime':self.due+1,'fundingRate':'0.001','markPrice':'100'};mark=self.mark(now,self.due+28800000)
  def crash(stage):
   if stage=='after_application':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):advance(f.store,position_id=self.pos['id'],mark_response=mark,received_ms=now,history=self.history(now,[row]),fault=crash)
  prior=f.store.get('position')['funding'];advance(f.store,position_id=self.pos['id'],mark_response=mark,received_ms=now,history=self.history(now,[row]))
  self.assertEqual(f.store.get('position')['funding'],prior);self.assertEqual(f.store.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],1)
 def test_changed_historical_event_is_never_overwritten(self):
  f=self.f;now=self.due+10;row={'symbol':'BTCUSDT','fundingTime':self.due+1,'fundingRate':'0.001','markPrice':'100'};mark=self.mark(now,self.due+28800000)
  advance(f.store,position_id=self.pos['id'],mark_response=mark,received_ms=now,history=self.history(now,[row]))
  with self.assertRaisesRegex(ValueError,'changed_requires_review'):record(f.store.db,{**row,'fundingRate':'0.002'},observed_ms=now+1)
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM funding_conflicts').fetchone()[0],1)
  self.assertFalse(covered(f.store,f.store.get('position'),now))
  with self.assertRaisesRegex(ValueError,'funding_conflict_requires_review'):
   advance(f.store,position_id=self.pos['id'],mark_response=mark,received_ms=now)
 def test_audit_missing_event_keeps_evidence_and_blocks(self):
  f=self.f;now=self.due+10;row={'symbol':'BTCUSDT','fundingTime':self.due+1,'fundingRate':'0.001','markPrice':'100'}
  advance(f.store,position_id=self.pos['id'],mark_response=self.mark(now,self.due+28800000),received_ms=now,history=self.history(now,[row]))
  self.assertEqual(audit(f.store,self.history(now,[row]))['checked'],1)
  with self.assertRaisesRegex(ValueError,'conflict_requires_review'):audit(f.store,self.history(now,[]))
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM funding_evidence').fetchone()[0],1)
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM funding_conflicts').fetchone()[0],1)
 def test_audit_extra_already_covered_event_is_conflict(self):
  f=self.f;now=self.due+10;row={'symbol':'BTCUSDT','fundingTime':self.due+1,'fundingRate':'0.001','markPrice':'100'}
  advance(f.store,position_id=self.pos['id'],mark_response=self.mark(now,self.due+28800000),received_ms=now,history=self.history(now,[row]))
  extra={'symbol':'BTCUSDT','fundingTime':self.f.now+1,'fundingRate':'0.002','markPrice':'100'}
  history={'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':self.f.now,'end_ms':now,'received_ms':now,'rows':[extra,row]}
  with self.assertRaisesRegex(ValueError,'conflict_requires_review'):audit(f.store,history)
