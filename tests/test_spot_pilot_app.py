import json,os,tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.app import Application
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.spot_public_data import SpotPublicData
from trading_bot.spot_pilot_app import SpotPilotApplication
from trading_bot.unified_queue import UnifiedQueue
from tests.test_runtime import symbol_info

class PilotApplicationTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);self.root=root
  s=RuntimeStore(root/'oldspot',{'mode':'paper','symbol':'BTCUSDT'});s.close()
  f=FuturesStore(root/'oldfutures');f.close()
  consolidate_snapshots(root/'oldspot',root/'oldfutures',root/'financial',captured_at=1)
  self.store=RuntimeStore(root/'financial',{'mode':'paper','symbol':'BTCUSDT'},shared=True);self.addCleanup(self.store.close)
  self.queue=UnifiedQueue(root/'queue');self.addCleanup(self.queue.close)
  Application(self.store,PaperExchange(),None);self.store.command(1,True,'/resume')
  self.reader=SpotPublicData()
  def public(path,params=None,**kwargs):
   now=int(time.time()*1000)
   if path=='/api/v3/time':return {'serverTime':now}
   if path=='/api/v3/trades':return [{'id':1,'time':now-100,'price':'100'}]
   if path=='/api/v3/ticker/price':return {'symbol':'BTCUSDT','price':'100'}
   if path=='/api/v3/exchangeInfo':return {'symbols':[symbol_info()]}
   raise AssertionError('unexpected endpoint')
  self.patcher=patch.object(self.reader,'get',side_effect=public);self.patcher.start();self.addCleanup(self.patcher.stop)
  self.app=SpotPilotApplication(self.store,PaperExchange(),self.reader,self.queue,control_dir=root,owner_uid=os.getuid())
 def request(self,name):
  p=self.root/name;p.write_text(json.dumps({'schema':1,'id':name.replace('.json',''),'expires':time.time()+300}));p.chmod(0o600)
 def test_worker_check_roundtrip_and_cash_start(self):
  self.request('check.json');self.app.tick()
  test=self.store.get('spot_test_cycle');self.assertEqual(test['phase'],'COMPLETE')
  self.assertEqual(len(self.store.get('spot_pilot')['entries']),0)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
  self.assertEqual(Decimal(self.store.get('position')['quantity']),0)
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  self.assertTrue(self.store.get('paused'))
  self.request('activate.json');self.app.tick()
  self.assertEqual(self.store.get('spot_pilot')['phase'],'active')
  self.assertFalse(self.store.get('paused'))
  self.assertGreater(self.store.get('spot_pilot')['next_execution_ms'],self.store.get('spot_pilot')['start_ms'])
 def test_manual_pause_after_startup_is_not_undone(self):
  self.request('check.json');self.app.tick()
  with self.queue.transaction():self.queue.db.execute("INSERT INTO unified_updates VALUES(99,'fixture','/pause','spot')")
  self.request('activate.json');self.app.tick()
  self.assertTrue(self.store.get('paused'))
 def test_bad_adapter_does_not_change_pause(self):
  with self.store.transaction():self.store.set('paused',False)
  with self.assertRaisesRegex(Exception,'requires shared'):SpotPilotApplication(self.store,object(),self.reader,self.queue,owner_uid=os.getuid())
  self.assertFalse(self.store.get('paused'))
 def test_predecision_quote_cannot_be_used_for_execution(self):
  with patch('trading_bot.spot_pilot_app._fresh',return_value=(Decimal('100'),99,101)),patch('trading_bot.spot_pilot_app.time.sleep'):
   with self.assertRaisesRegex(Exception,'after decision'):self.app._quote_after(100)
 def test_status_does_not_confuse_historical_donchian_and_pilot(self):
  from trading_bot.telegram_reports import status,daily_report,signal_report
  with self.store.transaction():
   self.store.set('donchian60',{'start_ms':1,'next_boundary_ms':2})
  for report in (status(self.store),daily_report(self.store),signal_report(self.store)):
   self.assertIn('btc-trend1-48-24-v1',report)
   self.assertIn('преимущество не доказано',report)
   self.assertIn('из 3',report)
   self.assertNotIn('Стратегия: Donchian60',report)
 def test_hour_signal_entry_and_stop_while_paused(self):
  from trading_bot.spot_pilot_rules import BAR
  start=201*BAR;clock=[start/1000];market=['100']
  def now():clock[0]+=.01;return clock[0]
  def public(path,params=None,**kwargs):
   stamp=int(now()*1000)
   if path=='/api/v3/time':return {'serverTime':stamp}
   if path=='/api/v3/trades':return [{'id':stamp,'time':stamp,'price':market[0]}]
   if path=='/api/v3/ticker/price':return {'symbol':'BTCUSDT','price':market[0]}
   if path=='/api/v3/exchangeInfo':return {'symbols':[symbol_info()]}
   if path=='/api/v3/klines':
    if market[0]=='97':raise RuntimeError('candles unavailable')
    rows=[[i*BAR,'100','101','99','100','1',(i+1)*BAR-1] for i in range(1,201)]
    rows[-2][2]='103';rows[-2][4]='102';return rows
   raise AssertionError(path)
  with self.store.transaction():
   state=self.store.get('spot_pilot');state.update(phase='active',start_ms=1,next_execution_ms=start);self.store.set('spot_pilot',state);self.store.set('paused',False)
  self.app.clock=now
  with patch.object(self.reader,'get',side_effect=public),patch('time.time',side_effect=now):
   self.app.tick()
   self.assertEqual(len(self.store.get('spot_pilot')['entries']),1)
   self.assertGreater(Decimal(self.store.get('position')['quantity']),0)
   with self.store.transaction():self.store.set('paused',True)
   market[0]='97';clock[0]=(start+4*BAR)/1000;self.app.tick()
  self.assertEqual(Decimal(self.store.get('position')['quantity']),0)
  self.assertTrue(self.store.get('paused'))
  self.assertLess(Decimal(self.store.get('spot_pilot')['realized_usdt']),0)
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])

if __name__=='__main__':unittest.main()
