import datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from trading_bot.ai_screens import render_screen
from trading_bot.unified_queue import UnifiedQueue

class AIScreens(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.p=Path(self.tmp.name)/'report.json';self.now=1800000000
  stamp=lambda n:dt.datetime.fromtimestamp(n,dt.timezone.utc).isoformat()
  self.d={'schema':1,'data_utc':stamp(self.now),'answer_utc':stamp(self.now+1),'expires_utc':stamp(self.now+300),'metrics':'close','coverage':'неполное покрытие','summary':'ИИ: TREND_UP; baseline того же среза: RANGE','risks':'reserve','sources':[{'title':'ФРС','url':'https://example.org/news','published_utc':stamp(self.now-60),'observed_utc':stamp(self.now)}],'complete':True,'shadow_details':{'model':'TREND_UP','baseline':'RANGE','reasons':['Тренд растёт'], 'uncertainties':['Источник неполный'],'features':{'close_4h':100.123,'spread_bps':2.5},'budget_display':'Резерв $0.01; остаток $0.24','schedule':'Следующий пересмотр 20:00 Баку.'}}
  self.write()
 def write(self):self.p.write_text(json.dumps(self.d),encoding='utf8')
 def test_views_and_expiry(self):
  main=render_screen('main',self.p,self.now+2);self.assertIn('LONG',main);self.assertIn('диапазон',main);self.assertIn('не позиция',main);self.assertLess(len(main),1000);self.assertNotIn('https://',main)
  self.assertIn('срок истёк',render_screen('main',self.p,self.now+300));self.assertIn('Тренд растёт',render_screen('why',self.p,self.now+2));self.assertIn('USDT',render_screen('metrics',self.p,self.now+2));self.assertIn('https://',render_screen('sources',self.p,self.now+2));self.assertIn('$0.24',render_screen('costs',self.p,self.now+2))
 def test_absent_corrupt_and_incomplete(self):
  self.p.write_text('{');self.assertIn('повреждён',render_screen(path=self.p));self.p.unlink();self.assertIn('отсутствует',render_screen(path=self.p))
  self.d['complete']=False;self.d['diagnostic_only']=True;self.write();self.assertIn('Ожидание',render_screen(path=self.p,now=self.now+2))
 def test_buttons_are_authorized_read_only_and_replay_is_safe(self):
  q=UnifiedQueue(Path(self.tmp.name)/'queue.sqlite');self.addCleanup(q.close)
  with patch('trading_bot.ai_screens.REPORT',self.p),patch('research.adaptive_ai_executor._send',side_effect=AssertionError('network forbidden')):
   q.receive(1,True,'/ai',now=self.now);rows=q.db.execute("select value from unified_state where key like 'inline-action:%'").fetchall();self.assertEqual(len(rows),6)
   token=q.db.execute("select key from unified_state where key like 'inline-action:%' limit 1").fetchone()[0].split(':',1)[1]
   q.receive(2,False,'',callback_data=token,now=self.now);q.receive(3,True,'',callback_data=token,now=self.now);q.receive(3,True,'',callback_data=token,now=self.now)
   self.assertEqual(q.db.execute('select count(*) from unified_jobs').fetchone()[0],0)
