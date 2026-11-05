import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tools import approved_ai_followup as f
from trading_bot.research_api_budget import APP_ID
from trading_bot.research_api_budget import paid_analysis
from trading_bot.market_research import build_snapshot

class FollowupTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.budget=self.root/'experiment-budget.db';d=sqlite3.connect(self.budget);d.execute(f'pragma application_id={APP_ID}');d.execute('create table api_attempts(request_id text,reserved_micro_usd integer,created_at real,result_json text)');d.execute("insert into api_attempts values('old',3630,1,null)");d.commit();d.close();(self.root/'interactive-once.lock').write_text('old');self.snap=self.root/'snapshot.json';self.snap.write_text(json.dumps({'snapshot_id':f.SNAPSHOT}))
 def test_explicit_approval_and_one_shot_keep_original(self):
  with patch.object(f,'SNAPSHOT_SHA256',hashlib.sha256(self.snap.read_bytes()).hexdigest()),patch.object(f,'prepare_request',return_value={'max_output_tokens':2000}),patch.object(f,'paid_analysis',return_value={'ok':True}) as paid:
   with self.assertRaises(Exception):f.run(self.snap,self.root)
   self.assertEqual(f.run(self.snap,self.root,approved=True,transport=lambda req:None),{'ok':True});self.assertTrue((self.root/'interactive-once.lock').exists())
   with self.assertRaises(FileExistsError):f.run(self.snap,self.root,approved=True,transport=lambda req:None)
   self.assertEqual(paid.call_count,1)
 def test_missing_or_changed_original_budget_refused(self):
  (self.root/'interactive-once.lock').unlink()
  with self.assertRaises(Exception):f.run(self.snap,self.root,approved=True)
 def test_usage_kept_when_response_is_incomplete(self):
  fixture=Path(__file__).resolve().parents[1]/'research/fixtures/ai-observer-synthetic.json';snapshot=build_snapshot(json.loads(fixture.read_text()))
  with self.assertRaises(Exception):paid_analysis(snapshot,self.root/'isolated.db',enabled=True,transport=lambda _:{'status':'incomplete','usage':{'input_tokens':123,'output_tokens':5,'secret':'ignored'}})
  d=sqlite3.connect(self.root/'isolated.db');usage=json.loads(d.execute('select usage_json from api_usage').fetchone()[0]);d.close();self.assertEqual(usage,{'input_tokens':123,'output_tokens':5})
