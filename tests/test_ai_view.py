import datetime as dt,json,tempfile,unittest
from pathlib import Path
from trading_bot.ai_view import render
class AIViewTest(unittest.TestCase):
 def test_absence_stale_incomplete_and_separate_metrics(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'report.json';self.assertIn('ещё не выполнен',render(p));now=1800000000;stamp=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat();d={'schema':1,'data_utc':stamp,'answer_utc':stamp,'metrics':'RSI 10','coverage':'нет макро','summary':'неопределённо','risks':'волатильность','sources':[],'complete':True};p.write_text(json.dumps(d))
   result=render(p,now);self.assertIn('Вычисленные метрики',result);self.assertIn('Вывод модели',result);self.assertIn('УСТАРЕЛО',render(p,now+86401))
   d['complete']=False;p.write_text(json.dumps(d));self.assertIn('НЕПОЛНЫЙ',render(p,now));self.assertIn('НЕПОЛНЫЙ',render(p,now+86401));p.write_text('{');self.assertIn('повреждён',render(p,now))
