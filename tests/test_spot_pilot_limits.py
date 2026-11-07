import unittest
from trading_bot.spot_pilot_limits import entry_allowed,record_entry,record_close,validate_intent

class PilotLimitsTest(unittest.TestCase):
 def state(self):return {'version':'btc-trend1-48-24-v1','phase':'active','entries':[],'realized_usdt':'0','open_position_id':None}
 def test_three_entries_persist_and_replay_does_not_count_twice(self):
  s=self.state()
  for i in range(3):
   self.assertTrue(record_entry(s,client_id=str(i),position_id=str(i)))
   self.assertFalse(record_entry(s,client_id=str(i),position_id=str(i)))
   record_close(s,position_id=str(i),fill_id='exit'+str(i),pnl='0',known=True,remaining='0')
  self.assertEqual(entry_allowed(s),(False,'pilot_entry_limit'))
 def test_cumulative_loss_stops_entries_but_close_remains_possible(self):
  s=self.state();record_entry(s,client_id='b',position_id='p')
  record_close(s,position_id='p',fill_id='s1',pnl='-1.01',known=True,remaining='0.1')
  self.assertEqual(entry_allowed(s),(False,'pilot_loss_limit'))
  self.assertFalse(record_close(s,position_id='p',fill_id='s1',pnl='-1.01',known=True,remaining='0.1'))
  self.assertTrue(record_close(s,position_id='p',fill_id='s2',pnl='0',known=True,remaining='0'))
  self.assertEqual(s['realized_usdt'],'-1.01')
 def test_unknown_close_never_becomes_zero(self):
  s=self.state();record_entry(s,client_id='b',position_id='p')
  record_close(s,position_id='p',fill_id='s',pnl='0',known=False,remaining='0')
  self.assertEqual(s['realized_usdt'],'0');self.assertIsNone(s['open_position_id'])
  self.assertEqual(entry_allowed(s),(False,'pilot_realized_unconfirmed'))
  self.assertIsNone(s['close_fills']['s']['pnl'])
 def test_intent_must_be_after_start_and_quote_must_survive_sqlite_wait(self):
  s=self.state();s['start_ms']=1000;s['intent']={'action':'BUY','signal_boundary_ms':3600000,'execution_boundary_ms':4500000,'committed_ms':3601000,'quote_event_ms':4500100,'quote_received_ms':4500200}
  self.assertTrue(validate_intent(s,quote_at=4500.2,now_ms=4500300))
  with self.assertRaisesRegex(ValueError,'stale'):validate_intent(s,quote_at=4500.2,now_ms=4506000)
  s['start_ms']=3600000
  with self.assertRaisesRegex(ValueError,'stale'):validate_intent(s,quote_at=4500.2,now_ms=4500300)
