import json,os,tempfile,unittest
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
  initialize(self.root/'ai',source='AI',start_ms=self.data_ms-1000);initialize(self.root/'baseline',source='baseline',start_ms=self.data_ms-1000)
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
  self.now=due+10;self.markets['futures']['quote'].update(price='103',event_ms=self.now,received_ms=self.now,next_due_ms=due+28800000)
  self.markets['futures']['funding_history']={'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':due-1,'end_ms':self.now,'received_ms':self.now,'rows':[{'symbol':'BTCUSDT','fundingTime':due+1,'fundingRate':'0.001','markPrice':'100'}]}
  self.assertEqual(self.run_cycle('ai',self.private/'missing')['status'],'protection_exit')
