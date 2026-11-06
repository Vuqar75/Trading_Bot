"""Synthetic same-data comparison; no profitability inference."""
import json,sqlite3,tempfile,unittest
from pathlib import Path
import tests.test_adaptive_atomic_spot as fixture
from research.adaptive_route_state import RouteState
from research.adaptive_shadow import baseline
from research.adaptive_spot_worker import process_entry,process_stop
from research.adaptive_decision_queue import record as record_decision
from tests.test_runtime import symbol_info

class SameSnapshotComparison(unittest.TestCase):
 def test_model_and_baseline_share_inputs_but_not_financial_state(self):
  base=baseline([{'close':100} for _ in range(19)]+[{'close':101}]);self.assertEqual(base,'RANGE')
  results=[];datasets=[];logdir=tempfile.TemporaryDirectory();self.addCleanup(logdir.cleanup)
  log=sqlite3.connect(Path(logdir.name)/'comparison.sqlite');self.addCleanup(log.close)
  log.execute('create table observations(source text primary key,snapshot text,mode text,route text,signal text,result text,orders integer,fees text,pnl text,exposure text)')
  for source,mode,strategy in (('AI','TREND_UP','trend1'),('baseline',base,'range')):
   f=fixture.AtomicSpotTest('test_real_shared_fill_and_reservation');f.setUp()
   try:
    state=RouteState(f.root/'route',clock=lambda:f.now)
    decision=dict(f.decision,mode=mode);f.bind(decision,source);record_decision(f.q,decision,source=source,symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=f.now);signal={'source':source,'symbol':'BTCUSDT','snapshot_id':'s','strategy':strategy,'direction':'LONG'}
    args={'decision':decision,'signal':signal,'data_ms':f.now-1000,'decision_version':'regular-regime-horizon-v2','bars':f.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':f.now,'filters':symbol_info(),'filters_ms':f.now,'clock':lambda:f.now}
    datasets.append((f.bars,args['quote'],args['filters'],decision['snapshot_id']))
    try:
     try:out=process_entry(f.q,f.store,state,**args)['status']
     except ValueError as e:out=str(e)
     quantity=f.store.get('position')['quantity']
     if out=='OPEN':
      with f.q.transaction():
       f.q.db.execute("INSERT INTO unified_updates VALUES(2,'exit','/research_adaptive_exit','spot')")
       f.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(2,'spot','/research_adaptive_exit',?)",(json.dumps({'action_id':'stop','entry_action':'entry'}),))
      self.assertEqual(process_stop(f.q,f.store,state,quote='97',quote_ms=f.now,clock=lambda:f.now)['status'],'IDLE')
     from decimal import Decimal
     fees=sum((Decimal(json.loads(r[0])['commission']) for r in f.store.db.execute('select payload from runtime_fills')),Decimal(0))
     record={'source':source,'mode':mode,'result':out,'orders':f.store.db.execute('select count(*) from runtime_orders').fetchone()[0],'quantity':quantity,'cash':f.store.get('cash'),'fees':str(fees),'pnl':str(f.store.get('pnl'))}
     results.append(record);log.execute('insert into observations values(?,?,?,?,?,?,?,?,?,?)',(source,'s',mode,'spot',strategy,out,record['orders'],record['fees'],record['pnl'],record['quantity']));log.commit()
    finally:state.close()
   finally:f.doCleanups()
  self.assertEqual(datasets[0],datasets[1]);self.assertEqual(results[0]['result'],'OPEN');self.assertEqual(results[1]['result'],'NO_SIGNAL')
  self.assertEqual((results[0]['orders'],results[1]['orders']),(2,0));self.assertNotEqual(results[0]['quantity'],results[1]['quantity'])
  self.assertNotEqual(results[0]['pnl'],results[1]['pnl']);self.assertEqual(log.execute('select count(distinct snapshot) from observations').fetchone()[0],1)
  print('SYNTHETIC_ADAPTIVE_COMPARISON',json.dumps(results,sort_keys=True))
