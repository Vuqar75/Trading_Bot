import json,os,sqlite3,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from research import adaptive_auto_once as a
from research.adaptive_shadow import PERIOD
class AutoOnceTests(unittest.TestCase):
 def test_window_and_boundary_are_strict(self):
  b=100*PERIOD
  for n in (b+119999,b+300001,b-1):
   with self.assertRaisesRegex(ValueError,'window'):a.check_window(b,n)
  a.check_window(b,b+120000);a.check_window(b,b+300000)
  with self.assertRaisesRegex(ValueError,'window'):a.check_window(b+1,b+120000)
 def test_one_shot_uses_key_only_in_memory_and_publishes(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp);boundary=100*PERIOD;seen=[]
   def fake_run(snapshot,baseline,path,**kwargs):
    self.assertEqual(os.environ.get('TRADING_BOT_AI'),'fake-secret');self.assertTrue(kwargs['next_authorized']);seen.append('run')
    return {'response':{'mode':'TREND_UP'},'reserved_micro_usd':2088,'usage':{'input_tokens':1}}
   def fake_export(db,path):path.write_text('{}');seen.append('export')
   with patch.object(a,'STATE',state),patch.object(a,'check_budget'),patch.object(a,'migrate'),patch.object(a,'run',fake_run),patch.object(a,'export',fake_export):
    result=a.one_shot(boundary,clock=lambda:boundary+120001,fetch=lambda:({'latest_4h_ms':boundary-PERIOD},'RANGE'),credential=lambda:'fake-secret',publish=lambda p:seen.append('publish'))
   self.assertEqual(seen,['run','export','publish']);self.assertEqual(result['baseline'],'RANGE');self.assertNotEqual(os.environ.get('TRADING_BOT_AI'),'fake-secret');self.assertNotIn('fake-secret',str(result))
 def test_no_request_if_wrong_window_or_candle(self):
  b=100*PERIOD;calls=[]
  with self.assertRaisesRegex(ValueError,'window'):a.one_shot(b,clock=lambda:b+1,credential=lambda:calls.append(1))
  self.assertEqual(calls,[])
  with patch.object(a,'check_budget'),patch.object(a,'migrate'):
   with self.assertRaisesRegex(ValueError,'candle'):a.one_shot(b,clock=lambda:b+120000,credential=lambda:'key',fetch=lambda:({'latest_4h_ms':0},'RANGE'))
 def test_local_recovery_without_second_http(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp);b=100*PERIOD;db=sqlite3.connect(state/'experiment-budget.db');db.execute('create table adaptive_responses(request_id text)');db.execute('insert into adaptive_responses values (?)',('adaptive:'+str(b),));db.commit();db.close();calls=[]
   def fail(*args,**kwargs):calls.append('one_attempt');raise RuntimeError('failure after save')
   def recovery(*args):calls.append('local_recovery');return {'response':{'mode':'NO_TRADE'},'reserved_micro_usd':2000,'usage':{}}
   with patch.object(a,'STATE',state),patch.object(a,'check_budget'),patch.object(a,'migrate'),patch.object(a,'run',fail),patch.object(a,'recover',recovery),patch.object(a,'export',lambda db,path:path.write_text('{}')):
    result=a.one_shot(b,clock=lambda:b+120000,credential=lambda:'key',fetch=lambda:({'latest_4h_ms':b-PERIOD},'RANGE'),publish=lambda p:calls.append('publish'))
   self.assertEqual(calls,['one_attempt','local_recovery','publish']);self.assertEqual(result['mode'],'NO_TRADE')
 def test_incomplete_report_cannot_be_published(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'report.json';p.write_text(json.dumps({'complete':False,'summary':'baseline того же среза: RANGE'}));calls=[]
   with self.assertRaisesRegex(ValueError,'comparison_missing'):a.publish_report(p,runner=lambda *x,**y:calls.append(1))
   self.assertEqual(calls,[])
 def test_publication_prepares_guarded_vm_script_without_secret(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'report.json';p.write_text(json.dumps({'complete':True,'summary':'ИИ: TREND_UP; baseline того же среза: RANGE'}));calls=[]
   def paths(args,**kwargs):return subprocess.CompletedProcess(args,0,str(args[-1]),'')
   def send(args,**kwargs):
    script=Path(args[-1]).read_text();compile(script,'remote','exec');self.assertIn('installed version changed',script);self.assertIn('report is not newer',script);self.assertNotIn('fake-secret',script);calls.append('published');return subprocess.CompletedProcess(args,0,'AI_COMPARISON_DISPLAY_VERIFIED','')
   with patch.object(a.shutil,'which',side_effect=lambda name:'/fake/'+name),patch.object(a.subprocess,'run',side_effect=paths),patch.dict(os.environ,{'TRADING_BOT_AI':'fake-secret'}):a.publish_report(p,runner=send)
   self.assertEqual(calls,['published'])
 def test_credential_is_read_from_captured_pipe_without_arguments(self):
  calls=[]
  def fake(args,**kwargs):
   calls.append((args,kwargs))
   if args[0].endswith('wslpath'):return subprocess.CompletedProcess(args,0,'C:\\helper.py','')
   return subprocess.CompletedProcess(args,0,b'fake-secret',b'')
  with patch.object(a.shutil,'which',side_effect=lambda name:'/fake/'+name),patch.object(a.subprocess,'run',side_effect=fake):self.assertEqual(a.secret_from_credential_manager(),'fake-secret')
  self.assertTrue(all('fake-secret' not in str(args) for args,kwargs in calls));self.assertTrue(all(kwargs['capture_output'] for args,kwargs in calls))
