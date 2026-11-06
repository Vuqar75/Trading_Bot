import datetime as dt,json,tempfile,unittest
from pathlib import Path
from trading_bot.ai_view import render,shadow
class AIViewTest(unittest.TestCase):
 def test_absence_stale_incomplete_and_separate_metrics(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'report.json';self.assertIn('ещё не выполнен',render(p));now=1800000000;stamp=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat();d={'schema':1,'data_utc':stamp,'answer_utc':stamp,'metrics':'RSI 10','coverage':'нет макро','summary':'неопределённо','risks':'волатильность','sources':[],'complete':True};p.write_text(json.dumps(d))
   result=render(p,now);self.assertIn('Вычисленные метрики',result);self.assertIn('Вывод модели',result);self.assertIn('УСТАРЕЛО',render(p,now+86401))
   d['complete']=False;p.write_text(json.dumps(d));self.assertIn('НЕПОЛНЫЙ',render(p,now));self.assertIn('НЕПОЛНЫЙ',render(p,now+86401));p.write_text('{');self.assertIn('повреждён',render(p,now))
 def test_shadow_never_claims_ai_decision_or_enables_entries(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'status.json';t=1800000000000;d={'schema':1,'checked_ms':t,'data_ms':t,'baseline':'TREND_DOWN','ai_mode':None,'entry_allowed':False,'entry_reason':'no_qualified_strategy_and_budget_executor_unavailable','features':{'close_4h':100},'coverage':'Новости неполны','sources':[]};p.write_text(json.dumps(d))
   result=shadow(p,t/1000);self.assertIn('ИИ: ответа нет',result);self.assertIn('TREND_DOWN',result);self.assertIn('входы запрещены',result);self.assertIn('устарели',shadow(p,t/1000+3601))
   d['entry_allowed']=True;p.write_text(json.dumps(d));self.assertIn('недоступны',shadow(p,t/1000))
