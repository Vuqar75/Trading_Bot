import json,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from tests.test_runtime import symbol_info
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.app import Application
from research.adaptive_route_state import RouteState
from research.adaptive_route_policy import decision_digest,candle_digest
from research.adaptive_decision_queue import record
from research.adaptive_spot_worker import process_entry,process_short,process_stop,process_short_stop
from research.adaptive_protection_monitor import observe

class TransitionTest(unittest.TestCase):
 def test_long_range_short_never_overlaps_and_uses_current_decision(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);spot=RuntimeStore(p/'spot',{'mode':'paper','symbol':'BTCUSDT'});spot.close();f=FuturesStore(p/'future',fee_bps='10',slippage_bps='5');f.close()
   financial=p/'financial';queue_path=p/'queue';consolidate_snapshots(p/'spot',p/'future',financial,captured_at=1)
   shared=SharedPaperStore(financial);CrossPaperBook(shared);MarginPaperBook(shared.ledger);shared.close()
   q=UnifiedQueue(queue_path);s=RuntimeStore(financial,{'mode':'paper','symbol':'BTCUSDT'},shared=True);f=FuturesStore(financial,shared=True,fee_bps='10',slippage_bps='5')
   now=[197*900000];state=RouteState(p/'route',clock=lambda:now[0]);clock=lambda:now[0]
   try:
    Application(s,PaperExchange(),None);s.command(1,True,'/resume');f.command(1,True,'/resume')
    decision={'mode':'TREND_UP','snapshot_id':'up','data_ms':now[0]-1000,'expires_ms':now[0]+10000,'reasons':['fixture'],'uncertainties':['fixture']}
    record(q,decision,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=now[0])
    with q.transaction():
     q.db.execute("INSERT INTO unified_updates VALUES(1,'up','/research_adaptive_entry','spot')")
     q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(1,'spot','/research_adaptive_entry',?)",(json.dumps({'action_id':'long','decision_sha256':decision_digest(decision)}),))
    bar=900000;history=[(i*bar,100.,101.,99.,100.) for i in range(196)];history[-1]=(195*bar,100.,102.,99.,101.)
    with q.transaction():q.db.execute('UPDATE unified_jobs SET payload=? WHERE id=1',(json.dumps({'action_id':'long','decision_sha256':decision_digest(decision),'candles_sha256':candle_digest(history,(196*bar,100.,101.,99.,100.))}),))
    spot_signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'up','strategy':'trend1','direction':'LONG'}
    a={'decision':decision,'signal':spot_signal,'data_ms':decision['data_ms'],'decision_version':'regular-regime-horizon-v2','bars':history,'skip_bar':(196*bar,100.,101.,99.,100.),'quote':'100','quote_ms':now[0],'filters':symbol_info(),'filters_ms':now[0],'clock':clock}
    self.assertEqual(process_entry(q,s,state,**a)['status'],'OPEN');long_id=s.get('position')['id']
    range_decision=dict(decision,mode='RANGE',snapshot_id='range',data_ms=now[0],expires_ms=now[0]+10000)
    record(q,range_decision,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=now[0])
    self.assertEqual((state.status()['strategy'],s.get('position')['id']),('trend1',long_id))
    now[0]=201*bar
    short_decision=dict(decision,mode='TREND_DOWN',snapshot_id='down',data_ms=now[0]-1000,expires_ms=now[0]+10000)
    record(q,short_decision,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=now[0])
    with q.transaction():
     q.db.execute("INSERT INTO unified_updates VALUES(2,'down','/research_adaptive_entry','futures')")
     q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(2,'futures','/research_adaptive_entry',?)",(json.dumps({'action_id':'short','decision_sha256':decision_digest(short_decision)}),))
    history.extend([(196*bar,100.,101.,99.,100.),(197*bar,100.,101.,99.,100.),(198*bar,100.,101.,99.,100.),(199*bar,100.,101.,98.,99.)])
    funding={'symbol':'BTCUSDT','complete':True,'observed_ms':now[0],'next_due_ms':now[0]+28800000,'source':'synthetic'}
    with q.transaction():q.db.execute('UPDATE unified_jobs SET payload=? WHERE update_id=2',(json.dumps({'action_id':'short','decision_sha256':decision_digest(short_decision),'candles_sha256':candle_digest(history[-196:],(200*bar,100.,101.,99.,100.)),'funding_sha256':decision_digest(funding)}),))
    filters=symbol_info();filters.update(contractType='PERPETUAL',marginAsset='USDT')
    short_signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'down','strategy':'trend1','direction':'SHORT'}
    b={'decision':short_decision,'signal':short_signal,'data_ms':short_decision['data_ms'],'decision_version':'regular-regime-horizon-v2','bars':history[-196:],'skip_bar':(200*bar,100.,101.,99.,100.),'quote':'100','quote_ms':now[0],'filters':filters,'filters_ms':now[0],'funding_evidence':funding,'clock':clock}
    with self.assertRaisesRegex(ValueError,'exposure_or_pending'):process_short(q,f,state,**b)
    self.assertIsNone(f.get('position'));self.assertEqual(s.get('position')['id'],long_id)
    self.assertEqual(observe(q,s,state,quote='97',quote_ms=now[0],clock=clock)['trigger'],'stop')
    self.assertEqual(process_stop(q,s,state,quote='97',quote_ms=now[0],clock=clock)['status'],'IDLE')
    self.assertEqual(process_short(q,f,state,**b)['status'],'OPEN');self.assertEqual(f.get('position')['direction'],'SHORT');self.assertEqual(Decimal(s.get('position')['quantity']),0)
    now[0]+=2000;no_trade=dict(short_decision,mode='NO_TRADE',snapshot_id='off',data_ms=now[0]-1000,expires_ms=now[0]+10000)
    record(q,no_trade,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=now[0])
    self.assertEqual(f.get('position')['direction'],'SHORT');self.assertEqual(state.status()['strategy'],'trend1')
    f.apply_funding(f.get('position')['id'],[],now[0]);self.assertEqual(observe(q,f,state,quote='103',quote_ms=now[0],clock=clock)['trigger'],'stop')
    self.assertEqual(process_short_stop(q,f,state,quote='103',quote_ms=now[0],clock=clock)['status'],'IDLE');self.assertIsNone(f.get('position'))
    self.assertTrue(s.shared.ledger.reconcile()['balanced'])
   finally:state.close();f.close();s.close();q.close()
