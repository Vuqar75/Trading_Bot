import json,os,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from research.adaptive_experiment_init import initialize
from research.adaptive_experiment_cycle import cycle
from research.adaptive_snapshot_proof import bind,capture
from research.adaptive_shadow import PERIOD
from tests import test_adaptive_shadow as shadow_fixture
from tests.test_runtime import symbol_info

class CycleTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.now=197*900000;self.data_ms=self.now-1800000
  f=shadow_fixture.ShadowTest();f.setUp();self.addCleanup(f.doCleanups);f.now=self.data_ms
  self.captured=capture(f.fetch,clock=lambda:self.data_ms,news_fetch=lambda **kw:([],[]))
  snapshot=self.captured['snapshot'];self.decision={'mode':'TREND_UP','snapshot_id':snapshot['snapshot_id'],'data_ms':self.data_ms,'expires_ms':(self.data_ms//PERIOD+1)*PERIOD,'reasons':['fixture'],'uncertainties':['fixture']}
  self.ai=bind(self.captured,self.decision,self.data_ms+1,model='gpt-6-luna',request_sha256='0'*64)
  self.private=self.root/'published';self.private.mkdir(mode=0o700);self.apath=self.private/'ai.json';self.bpath=self.private/'base.json'
  self.apath.write_text(json.dumps(self.ai));self.apath.chmod(0o600);self.bpath.write_text(json.dumps(self.captured));self.bpath.chmod(0o600)
  initialize(self.root/'ai',source='AI',start_ms=self.data_ms//PERIOD*PERIOD-1);initialize(self.root/'baseline',source='baseline',start_ms=self.data_ms//PERIOD*PERIOD-1)
  bar=900000;bars=[(i*bar,100.,101.,99.,100.) for i in range(196)];bars[-1]=(195*bar,100.,102.,99.,101.)
  self.markets={'spot':{'candles':{'market':'spot','symbol':'BTCUSDT','source':'binance_spot_klines','bars':bars,'skip_bar':(196*bar,100.,101.,99.,100.)},'quote':{'symbol':'BTCUSDT','source':'binance_spot_trade','price':'100','event_ms':self.now,'received_ms':self.now},'filters':{'market':'spot','symbol':'BTCUSDT','filters':symbol_info(),'received_ms':self.now}}}
 def run_cycle(self,name,path):return cycle(self.root/name,source='AI' if name=='ai' else 'baseline',decision_path=path,publisher_uid=os.getuid(),markets=self.markets,clock=lambda:self.now)
 def test_shared_input_distinct_accounts_ai_entry_and_ai_independent_exit(self):
  ai=self.run_cycle('ai',self.apath);base=self.run_cycle('baseline',self.bpath)
  self.assertEqual(ai['status'],'entry_evaluated');self.assertEqual(ai['result']['status'],'OPEN')
  self.assertEqual(base['status'],'entry_evaluated');self.assertEqual(base['result']['status'],'NO_SIGNAL')
  self.now+=1000;self.markets['spot']['quote'].update(price='97',event_ms=self.now,received_ms=self.now)
  # Missing AI file cannot prevent the local protective exit.
  self.assertEqual(self.run_cycle('ai',self.private/'missing')['status'],'protection_exit')
 def test_preexisting_signal_does_not_trade(self):
  self.ai['answer_ms']=self.now-1000
  from research.adaptive_snapshot_proof import digest
  self.ai['sha256']=digest({k:v for k,v in self.ai.items() if k!='sha256'});self.apath.write_text(json.dumps(self.ai))
  result=self.run_cycle('ai',self.apath)
  self.assertEqual((result['status'],result['reason']),('entry_blocked','signal_predates_decision_or_start'))
 def test_short_funding_and_protection_without_ai(self):
  self.decision['mode']='TREND_DOWN';self.ai=bind(self.captured,self.decision,self.data_ms+1,model='gpt-6-luna',request_sha256='0'*64);self.apath.write_text(json.dumps(self.ai))
  bar=900000;bars=[(i*bar,100.,101.,99.,100.) for i in range(196)];bars[-1]=(195*bar,100.,101.,98.,99.)
  filters=symbol_info();filters.update(contractType='PERPETUAL',marginAsset='USDT');due=self.now+5000
  self.markets['futures']={'candles':{'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','bars':bars,'skip_bar':(196*bar,100.,101.,99.,100.)},'quote':{'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':self.now,'received_ms':self.now,'next_due_ms':due},'filters':{'market':'futures','symbol':'BTCUSDT','filters':filters,'received_ms':self.now}}
  opened=self.run_cycle('ai',self.apath);self.assertEqual(opened['result']['status'],'OPEN')
  self.now=due+10;self.markets['futures']['quote'].update(price='103',event_ms=self.now-1,received_ms=self.now-1,next_due_ms=due+28800000)
  self.markets['futures']['funding_history']={'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':due-1,'end_ms':self.now,'received_ms':self.now,'rows':[{'symbol':'BTCUSDT','fundingTime':due+1,'fundingRate':'0.001','markPrice':'100'}]}
  self.assertEqual(self.run_cycle('ai',self.private/'missing')['status'],'protection_exit')
 def test_new_epoch_rejects_mark_as_trade_price(self):
  self.decision['mode']='TREND_DOWN';self.ai=bind(self.captured,self.decision,self.data_ms+1,model='gpt-6-luna',request_sha256='0'*64);self.apath.write_text(json.dumps(self.ai))
  self.markets['futures']={'epoch_v2':True}
  result=self.run_cycle('ai',self.apath)
  self.assertEqual((result['status'],result['reason']),('entry_blocked','futures_trade_execution_price_unverified'))
 def test_v2_full_cycle_uses_trade_and_local_exit_when_ai_missing(self):
  self.check_v2_full_cycle('SHORT')
 def test_two_sided_long_v2_epoch_cycle_and_protection_without_ai(self):
  self.check_v2_full_cycle('LONG')
 def check_v2_full_cycle(self,direction):
  import sqlite3
  from research.adaptive_market_epoch import _schema,ingest_epoch
  from research.adaptive_route_policy import VERSION,FUTURES_VERSION
  policy=FUTURES_VERSION if direction=='LONG' else VERSION
  self.decision['mode']='TREND_UP' if direction=='LONG' else 'TREND_DOWN';self.ai=bind(self.captured,self.decision,self.data_ms+1,model='gpt-6-luna',request_sha256='0'*64);self.apath.write_text(json.dumps(self.ai))
  bar=900000;bars=[(i*bar,100.,101.,99.,100.) for i in range(196)];bars[-1]=(195*bar,100.,102.,98.,101. if direction=='LONG' else 99.)
  candles={'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','bars':bars,'skip_bar':(196*bar,100.,101.,99.,100.)}
  candles['raw_bars']=[[r[0],str(r[1]),str(r[2]),str(r[3]),str(r[4]),'10',r[0]+899999] for r in bars+[candles['skip_bar']]]
  db=sqlite3.connect(self.root/'market-evidence.db',isolation_level=None);_schema(db);db.execute("INSERT INTO market_epochs(market,start_ms,created_ms,proof,source) VALUES('futures',?,?,?,?)",(194*bar,194*bar,'{}','binance_usdm_klines_v2'));ingest_epoch(db,market='futures',candles=candles,observed_ms=self.now)
  from research.adaptive_candle_confirmation import compare,record_event
  def warm(t):return {'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','raw_bars':candles['raw_bars'][:-1],'received_ms':t,'server_ms':t-1,'bars':bars[:-1],'skip_bar':bars[-1]}
  proof=compare(warm(self.now-45000),warm(self.now-30000));record_event(db,1,{'state':'accepted','sample':proof['second'],'comparison':proof},clock=lambda:self.now-29000);db.close()
  filters=symbol_info();filters.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
  for r in filters['filters']:
   if r['filterType']=='MIN_NOTIONAL':r['minNotional']='50'
  self.markets['futures']={'epoch_v2':True,'candles':candles,'quote':{'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':self.now,'received_ms':self.now,'next_due_ms':self.now+28800000},'filters':{'market':'futures','symbol':'BTCUSDT','filters':filters,'received_ms':self.now}}
  def trade(**kw):return {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':1,'price':self.markets['futures']['quote']['price'],'event_ms':self.now,'received_ms':self.now}
  result=cycle(self.root/'ai',source='AI',decision_path=self.apath,publisher_uid=os.getuid(),markets=self.markets,clock=lambda:self.now,execution_fetch=trade,route_policy=policy)
  self.assertEqual(result['result']['status'],'OPEN')
  self.assertEqual(result['route']['direction'],direction)
  from research.adaptive_study_status import snapshot
  from research.adaptive_study_supervisor import write_status
  from research.adaptive_telegram_export import publish
  current=snapshot(self.root/'ai',quotes={'futures':self.markets['futures']['quote']},now_ms=self.now)
  base=snapshot(self.root/'baseline',quotes={},now_ms=self.now)
  write_status(self.root,{'schema':1,'checked_ms':self.now,'start_ms':self.data_ms//PERIOD*PERIOD-1,'code_version':'test','route_policy':policy,'experiments':{'ai':current,'baseline':base},'cycles':{'ai':result}})
  (self.root/'cycle.lock').touch();output=self.root/'public';output.mkdir(mode=0o700)
  publish(self.root,output,clock=lambda:self.now)
  data=json.loads((output/'snapshot.json').read_text())
  level=Decimal(data['experiments']['ai']['positions'][0]['protective_level'])
  entry=Decimal(data['experiments']['ai']['positions'][0]['entry'])
  self.assertEqual(level,entry*(Decimal('.98') if direction=='LONG' else Decimal('1.02')))
  self.assertEqual(data['route_policy'],policy)
  self.now+=1000;self.markets['futures']['quote'].update(price='97' if direction=='LONG' else '103',event_ms=self.now,received_ms=self.now)
  result=cycle(self.root/'ai',source='AI',decision_path=self.private/'missing',publisher_uid=os.getuid(),markets=self.markets,clock=lambda:self.now,execution_fetch=trade,route_policy=policy)
  self.assertEqual(result['status'],'protection_exit');self.assertEqual(result['result']['status'],'IDLE')
 def test_history_conflict_blocks_entry_but_not_fresh_price_exit(self):
  self.markets['spot']['entry_conflict']=True
  self.assertEqual(self.run_cycle('ai',self.apath)['reason'],'market_history_conflict')
  self.markets['spot']['entry_conflict']=False
  self.assertEqual(self.run_cycle('ai',self.apath)['result']['status'],'OPEN')
  self.now+=1000;self.markets['spot']['entry_conflict']=True
  self.markets['spot']['quote'].update(price='97',event_ms=self.now,received_ms=self.now)
  self.assertEqual(self.run_cycle('ai',self.private/'missing')['status'],'protection_exit')
