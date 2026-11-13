import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research import ai_limit_executor as e
from tests.test_ai_limit_brackets import snapshot,T


class LimitExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.state=Path(self.temp.name)
        for name in ('interactive-once.lock','approved-followup-once.lock'):(self.state/name).write_text('unchanged-test-lock')
        (self.state/'server-owner.json').write_text(json.dumps({'role':'ACTIVE_SERVER','migration_id':'fixture'}))
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            db.execute('PRAGMA application_id='+str(e.api.APP_ID))
            db.execute('CREATE TABLE api_attempts(request_id TEXT PRIMARY KEY,reserved_micro_usd INTEGER NOT NULL,created_at REAL NOT NULL,result_json TEXT)')
            db.execute('INSERT INTO api_attempts VALUES(?,?,?,NULL)',('original',3630,1))
        self.s,self.d=snapshot();self.calls=0
    def tearDown(self):self.temp.cleanup()
    def reply(self,request):
        self.calls+=1
        self.assertEqual(request['model'],'gpt-6-luna')
        return {'status':'completed','usage':{'input_tokens':500,'output_tokens':200},'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(self.d)}]}]}
    def run_one(self,**kwargs):return e.once(self.state,self.s,authorized=True,transport=self.reply,clock=lambda:T,**kwargs)
    def test_one_attempt_preserves_original_locks_and_reservation_and_reuses_saved_answer(self):
        result=self.run_one();self.assertEqual(self.calls,1)
        self.assertEqual(self.run_one(),result);self.assertEqual(self.calls,1)
        for name in ('interactive-once.lock','approved-followup-once.lock'):self.assertEqual((self.state/name).read_text(),'unchanged-test-lock')
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            self.assertEqual(db.execute("SELECT reserved_micro_usd,result_json FROM api_attempts WHERE request_id='original'").fetchone(),(3630,None))
            self.assertEqual(db.execute('SELECT count(*) FROM api_usage').fetchone()[0],1)
    def test_uncertain_send_never_retries_and_saved_response_recovers_without_http(self):
        for stage in ('after_send_claim','after_response','after_response_saved','before_usage'):
            with self.subTest(stage=stage):
                self.tearDown();self.setUp()
                def fault(point):
                    if point==stage:raise RuntimeError('crash')
                with self.assertRaises(RuntimeError):self.run_one(fault=fault)
                calls=self.calls
                if stage in ('after_response_saved','before_usage'):
                    result=e.recover(self.state,'bracket:'+self.s['snapshot_id']);self.assertEqual(result['decision'],self.d)
                else:
                    with self.assertRaisesRegex(ValueError,'no_retry'):self.run_one()
                self.assertEqual(self.calls,calls)
    def test_budget_and_authorization_block_before_transport(self):
        with self.assertRaisesRegex(ValueError,'explicit'):e.once(self.state,self.s,transport=self.reply)
        with sqlite3.connect(self.state/'experiment-budget.db') as db:db.execute('INSERT INTO api_attempts VALUES(?,?,?,NULL)',('prior',246000,2))
        with self.assertRaisesRegex(ValueError,'budget_exhausted'):self.run_one()
        self.assertEqual(self.calls,0)
    def test_invalid_model_response_is_retained_but_cannot_create_order(self):
        self.d['entry_level']='invented'
        with self.assertRaisesRegex(ValueError,'unknown_level'):self.run_one()
        with self.assertRaisesRegex(ValueError,'unknown_level'):self.run_one()
        self.assertEqual(self.calls,1)
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bracket_api_requests WHERE raw_response IS NOT NULL AND result IS NULL').fetchone()[0],1)


if __name__=='__main__':unittest.main()
