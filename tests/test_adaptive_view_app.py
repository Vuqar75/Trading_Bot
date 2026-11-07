import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from trading_bot.adaptive_view import render
from trading_bot.unified_queue import UnifiedQueue

class AppViewTest(unittest.TestCase):
 def test_render_and_callbacks_without_financial_actions(self):
  with tempfile.TemporaryDirectory() as tmp:
   now=int(time.time());p=Path(tmp)/'snapshot.json'
   account={'positions':[],'route':{'phase':'IDLE','decision_fresh':True},'cycle':{'status':'entry_blocked','reason':'market_history_conflict','protection':{'state':'no_position'}},'decision':{'mode':'TREND_DOWN'},'net_pnl_usdt':'0','ledger_balanced':True,'closed_trades':0}
   data={'schema':1,'start_ms':(now-100)*1000,'checked_ms':now*1000,'generated_ms':now*1000,'experiments':{'ai':account,'baseline':account},'data_errors':{'futures_history':'conflict'},'watermarks':{}}
   p.write_text(json.dumps(data));msg=render(path=p,now=now)
   self.assertIn('расхождение истории рынка',msg);self.assertIn('AI:',msg);self.assertIn('Baseline:',msg);self.assertIn('isolated • тренд SHORT',msg)
   self.assertIn('Сопровождение: позиции нет',render('health',path=p,now=now))
   q=UnifiedQueue(Path(tmp)/'queue.db')
   try:
    with patch('trading_bot.adaptive_view.render',return_value=msg):
     q.receive(1,False,'/adaptive',now=now);self.assertEqual(q.db.execute('select count(*) from unified_outbox').fetchone()[0],0)
     q.receive(2,True,'/adaptive',now=now);token=q.db.execute("select key from unified_state where key like 'inline-action:%' limit 1").fetchone()[0].split(':',1)[1]
     q.receive(3,True,'',callback_data=token,now=now);q.receive(3,True,'',callback_data=token,now=now)
     self.assertEqual(q.db.execute('select count(*) from unified_jobs').fetchone()[0],0)
   finally:q.close()
