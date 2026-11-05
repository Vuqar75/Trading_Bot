import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from trading_bot import donchian_view as v
from trading_bot.unified_queue import UnifiedQueue

class ViewTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'snapshot.json';self.now=time.time();at=v.dt.datetime.fromtimestamp(self.now,v.dt.timezone.utc).isoformat()
  rows=[{'symbol':s,'scenario':c,'kind':k,'cash':'1000','qty':'0','realized':'0','open_net':'0','net':'0'} for s in v.SYMBOLS for c in ('base','stress') for k in ('strategy','hold','cash')]
  self.data={'schema':1,'generated_utc':at,'status':{'start_utc':at,'issues':[],'last_input_received_utc':at,'source_protocol':'trade-time-v2','missed_events':0},'accounts':rows,'closed':{f'{c}:{s}':0 for c in ('base','stress') for s in v.SYMBOLS}};self.save()
 def tearDown(self):self.tmp.cleanup()
 def save(self):self.path.write_text(json.dumps(self.data))
 def test_scenarios_and_baselines(self):
  self.data['accounts'][0].update(qty='0.12345678912',realized='-1.25',open_net='0.2',net='-1.05');self.save()
  x=v.render('summary',self.path,self.now);self.assertIn('0.1234567891 BTC',x);self.assertIn('-1.05',x);self.assertNotIn('Повышенные',x);self.assertIn('закрыто сделок 0',x)
  x=v.render('baseline',self.path,self.now);self.assertIn('Повышенные',x);self.assertIn('Buy-and-hold',x);self.assertIn('Cash',x);self.assertLess(len(x),4096)
 def test_stale_corrupt_and_incomplete(self):
  self.assertIn('не подтверждён',v.render(path=self.path,now=self.now+1801));self.data['status']['issues']=['protocol_halted'];self.save();self.assertIn('не подтверждён',v.render(path=self.path,now=self.now))
  self.path.write_text('{');self.assertIn('повреждён',v.render(path=self.path,now=self.now));self.data['accounts'].pop();self.save();self.assertIn('повреждён',v.render(path=self.path,now=self.now))
 def test_authorization_replay_and_buttons_no_jobs(self):
  q=UnifiedQueue(self.root/'queue.db');q.receive(1,False,'/donchian');self.assertEqual(q.db.execute('select count(*) from unified_outbox').fetchone()[0],0)
  with patch('trading_bot.donchian_view.render',return_value='report'):
   q.receive(2,True,'/donchian',now=self.now);q.receive(2,True,'/donchian',now=self.now)
   self.assertEqual(q.db.execute('select count(*) from unified_jobs').fetchone()[0],0);self.assertEqual(q.db.execute('select count(*) from unified_outbox').fetchone()[0],1)
   token=q.db.execute("select key from unified_state where key like 'inline-action:%' limit 1").fetchone()[0].split('inline-action:')[1]
   q.receive(3,True,'',callback_data=token,now=self.now+1);self.assertEqual(q.db.execute('select count(*) from unified_jobs').fetchone()[0],0)
  q.close()
