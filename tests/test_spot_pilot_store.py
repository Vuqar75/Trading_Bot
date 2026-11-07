import tempfile,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.app import Application
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.testnet_observer import ObservationError

class PilotStoreTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.store=RuntimeStore(Path(self.tmp.name)/'financial.db',{'mode':'paper'});self.addCleanup(self.store.close)
  self.app=Application(self.store,PaperExchange(),None);self.store.command(1,True,'/resume')
  self.state={'version':'btc-trend1-48-24-v1','phase':'active','entries':[],'realized_usdt':'0','open_position_id':None,'start_ms':1000,'intent':{'action':'BUY','signal_boundary_ms':3600000,'execution_boundary_ms':4500000,'committed_ms':3601000,'quote_event_ms':4500100,'quote_received_ms':4500200}}
  with self.store.transaction():self.store.set('spot_pilot',self.state)
 def test_fills_and_manual_close_are_counted_atomically(self):
  with patch('trading_bot.runtime_store.time.time',return_value=4500.3):
   self.assertTrue(self.app.execute('BUY','.1',Decimal('100'),'pilot:1','PILOT1H_ENTRY',quote_at=4500.2))
   pos=self.store.get('position');state=self.store.get('spot_pilot')
   self.assertEqual(len(state['entries']),1);self.assertEqual(state['open_position_id'],pos['id'])
   self.assertTrue(self.app.execute('SELL','.1',Decimal('98'),'manual-fixture','TEST',pos['id']))
   state=self.store.get('spot_pilot');self.assertIsNone(state['open_position_id'])
   self.assertEqual(Decimal(state['realized_usdt']),Decimal('-0.2198'))
   row=self.store.db.execute("select * from runtime_orders where side='SELL'").fetchone()
   self.store.apply(row['client_id'],PaperExchange.inspect_saved(row));self.assertEqual(Decimal(self.store.get('spot_pilot')['realized_usdt']),Decimal('-0.2198'))
 def test_old_automatic_buy_cannot_bypass_pilot(self):
  with self.assertRaisesRegex(ObservationError,'prohibits other'):self.store.prepare('BUY','.1',Decimal('100'),'old','TEST')
 def test_prepared_old_entry_is_expired_after_pilot_activation(self):
  with self.store.transaction():self.store.set('spot_pilot',None)
  client=self.store.prepare('BUY','.1',Decimal('100'),'old','TEST')
  with self.store.transaction():self.store.set('spot_pilot',self.state)
  self.assertIsNone(self.store.claim(client));self.assertFalse(self.store.pending())
 def test_fresh_quote_rechecked_after_wait(self):
  with patch('trading_bot.runtime_store.time.time',return_value=4500.3):
   client=self.store.prepare('BUY','.1',Decimal('100'),'pilot:1','PILOT1H_ENTRY',quote_at=4500.2)
  with patch('trading_bot.runtime_store.time.time',return_value=4506):self.assertIsNone(self.store.claim(client))
  self.assertEqual(self.store.db.execute('select status from runtime_orders where client_id=?',(client,)).fetchone()[0],'EXPIRED')
  self.assertEqual(self.store.get('spot_pilot')['entries'],[])
 def test_owner_check_uses_normal_paper_fills_and_does_not_count_pilot(self):
  self.state['phase']='awaiting_check'
  check={'schema':1,'authorization':'owner_local_once','id':'case1','phase':'BUY_ARMED','quote_event_ms':4500100,'quote_received_ms':4500200}
  with self.store.transaction():self.store.set('spot_pilot',self.state);self.store.set('spot_test_cycle',check);self.store.set('paused',True)
  with patch('trading_bot.runtime_store.time.time',return_value=4500.3):
   self.assertTrue(self.app.execute('BUY','.1',Decimal('100'),'paper-check:case1:buy','SPOT_CHECK_BUY',quote_at=4500.2))
   check=self.store.get('spot_test_cycle');self.assertEqual(check['phase'],'BUY_FILLED');self.assertTrue(self.store.get('paused'))
   check.update(phase='SELL_ARMED',quote_event_ms=4500100,quote_received_ms=4500200)
   with self.store.transaction():self.store.set('spot_test_cycle',check)
   self.assertTrue(self.app.execute('SELL','.1',Decimal('99'),'paper-check:case1:sell','SPOT_CHECK_SELL',check['position_id'],quote_at=4500.2))
   result=self.store.get('spot_test_cycle');self.assertEqual(result['phase'],'COMPLETE');self.assertEqual(Decimal(result['net_pnl']),Decimal('-.1199'))
   self.assertEqual(self.store.get('spot_pilot')['entries'],[]);self.assertTrue(self.store.get('paused'))
   self.assertEqual(Decimal(self.store.get('position')['quantity']),0)
 def test_check_requires_auth_and_cannot_open_more_than_ten(self):
  with self.assertRaisesRegex(ObservationError,'authorized'):self.store.prepare('BUY','.1',Decimal('100'),'paper-check:x:buy','SPOT_CHECK_BUY',quote_at=4500.2)
