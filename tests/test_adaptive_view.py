import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from trading_bot import adaptive_view
from trading_bot.unified_queue import UnifiedQueue
from research.adaptive_study_supervisor import bootstrap,cycle_lock,run_once,write_status
from research.adaptive_telegram_export import publish

class AdaptiveViewTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.now=1800000000
  self.study=self.root/'study';self.out=self.root/'public';self.out.mkdir(mode=0o700)
  self.start=self.now*1000-100000;bootstrap(self.study,clock=lambda:self.start)
  with cycle_lock(self.study):pass
  data=run_once(self.study,ai_path=self.root/'missing-ai',baseline_path=self.root/'missing-baseline',publisher_uid=0,markets={},clock=lambda:self.now*1000)
  data.update(schema=1,start_ms=self.start,code_version='fixture',data_errors={},market_watermarks={},backup={'state':'fresh'})
  write_status(self.study,data);publish(self.study,self.out,clock=lambda:self.now*1000)
  self.path=self.out/'snapshot.json'
 def test_separate_accounts_and_empty_positions(self):
  data=json.loads(self.path.read_text());self.assertEqual(set(data['experiments']),{'ai','baseline'})
  msg=adaptive_view.render(path=self.path,now=self.now)
  self.assertIn('позиций нет',msg);self.assertIn('AI:',msg);self.assertIn('Baseline:',msg);self.assertIn('прибыльность не доказана',msg)
  self.assertLess(len(msg),3500)
 def test_unknown_negative_stale_and_corrupt(self):
  data=json.loads(self.path.read_text());a=data['experiments']['ai'];a['net_pnl_usdt']='-0.000001';a['net_after_known_api_usd']=None;self.path.write_text(json.dumps(data))
  self.assertIn('-меньше 0.0001',adaptive_view.render(path=self.path,now=self.now))
  self.assertIn('неизвестно',adaptive_view.render('results',path=self.path,now=self.now))
  self.assertIn('устарел',adaptive_view.render(path=self.path,now=self.now+181))
  self.path.write_text('{');self.assertIn('повреждён',adaptive_view.render(path=self.path,now=self.now))
 def test_open_position_pending_and_funding_wait_are_distinct(self):
  data=json.loads(self.path.read_text());a=data['experiments']['ai']
  a['positions']=[{'market':'futures','direction':'SHORT','quantity':'0.0001234567891','entry':'100','strategy':'trend1','protective_level':'102'}]
  a['route'].update(phase='EXIT_PENDING',pending_jobs=1,strategy='trend1')
  a['cycle']={'status':'entry_blocked','reason':'funding_coverage_missing','protection':{'funding_wait':{'trigger':'stop'}}}
  self.path.write_text(json.dumps(data));positions=adaptive_view.render('positions',path=self.path,now=self.now)
  self.assertIn('SHORT',positions);self.assertIn('ожидается выход',positions);self.assertIn('102',positions)
  self.assertIn('не объявлена закрытой',adaptive_view.render('why',path=self.path,now=self.now))
 def test_view_commands_authorization_callbacks_and_no_jobs(self):
  q=UnifiedQueue(self.root/'queue');self.addCleanup(q.close)
  with patch('trading_bot.adaptive_view.render',return_value='[Adaptive] fixture'):
   q.receive(1,False,'/adaptive',now=self.now)
   self.assertEqual(q.db.execute('select count(*) from unified_outbox').fetchone()[0],0)
   q.receive(2,True,'/adaptive',now=self.now)
   tokens=q.db.execute("select key from unified_state where key like 'inline-action:%'").fetchall();self.assertEqual(len(tokens),5)
   token=tokens[0][0].split(':',1)[1];q.receive(3,False,'',callback_data=token,now=self.now)
   q.receive(4,True,'',callback_data=token,now=self.now);q.receive(4,True,'',callback_data=token,now=self.now)
   self.assertEqual(q.db.execute('select count(*) from unified_jobs').fetchone()[0],0)
