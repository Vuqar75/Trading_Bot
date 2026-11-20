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
    def move_snapshot(self, stamp):
        from research.ai_limit_plan import digest
        self.s.update(as_of_ms=stamp, expires_ms=stamp+900000)
        self.s['quote'].update(event_ms=stamp, received_ms=stamp)
        self.s['snapshot_id']=digest({k:v for k,v in self.s.items() if k!='snapshot_id'})
        self.d.update(snapshot_id=self.s['snapshot_id'], valid_until_ms=stamp+600000)

    def test_unknown_reservation_survives_new_cycle_and_same_slot_rehash(self):
        def unavailable(request):
            raise RuntimeError('http503')
        with self.assertRaisesRegex(RuntimeError,'http503'):
            e.once(self.state,self.s,authorized=True,transport=unavailable,clock=lambda:T)
        original='bracket:'+self.s['snapshot_id']
        with self.assertRaisesRegex(ValueError,'uncertain_no_retry'):
            self.run_one()
        self.move_snapshot(T+1)
        with self.assertRaisesRegex(ValueError,'cycle_already_claimed'):
            e.once(self.state,self.s,authorized=True,transport=self.reply,clock=lambda:T+1)
        later=T+4*3600000
        self.move_snapshot(later)
        result=e.once(self.state,self.s,authorized=True,transport=self.reply,clock=lambda:later)
        self.assertTrue(result['admission']['allowed'])
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            amount, raw, saved=db.execute('SELECT reserved_micro_usd,raw_response,result FROM api_attempts '
                'JOIN bracket_api_requests ON id=request_id WHERE id=?',(original,)).fetchone()
            self.assertGreater(amount,0)
            self.assertEqual((raw,saved),(None,None))
            self.assertEqual(result['total_reserved_micro_usd'],3630+amount+result['reserved_micro_usd'])
        self.assertEqual(self.calls,1)

    def test_late_saved_response_recovers_after_independent_cycle(self):
        original='bracket:'+self.s['snapshot_id']
        times=iter((T,T+901000))
        def fault(stage):
            if stage=='after_response_saved':
                raise RuntimeError('interrupt')
        with self.assertRaisesRegex(RuntimeError,'interrupt'):
            e.once(self.state,self.s,authorized=True,transport=self.reply,clock=lambda:next(times),fault=fault)
        later=T+4*3600000
        self.move_snapshot(later)
        current=e.once(self.state,self.s,authorized=True,transport=self.reply,clock=lambda:later)
        self.assertTrue(current['admission']['allowed'])
        recovered=e.recover(self.state,original)
        self.assertEqual(recovered['admission'],{'allowed':False,'reason':'snapshot_expired'})
        self.assertEqual(e.recover(self.state,original),recovered)
        self.assertEqual(self.calls,2)
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM api_usage').fetchone()[0],2)

    def test_contending_writer_daily_limit_and_cooldown(self):
        with e.exclusive(self.state), self.assertRaisesRegex(ValueError,'already_running'):
            self.run_one()
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            db.execute("INSERT INTO api_attempts VALUES('other',100,?,NULL)",(T/1000-10,))
        with self.assertRaisesRegex(ValueError,'cooldown'):
            self.run_one()
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            db.executemany('INSERT INTO api_attempts VALUES(?,100,?,NULL)',
                           [('prior'+str(i),T/1000-20) for i in range(5)])
        with self.assertRaisesRegex(ValueError,'daily_attempt_cap'):
            self.run_one()
        self.assertEqual(self.calls,0)

    def test_budget_and_authorization_block_before_transport(self):
        with self.assertRaisesRegex(ValueError,'explicit'):e.once(self.state,self.s,transport=self.reply)
        with sqlite3.connect(self.state/'experiment-budget.db') as db:db.execute('INSERT INTO api_attempts VALUES(?,?,?,NULL)',('prior',246000,2))
        with self.assertRaisesRegex(ValueError,'budget_exhausted'):self.run_one()
        self.assertEqual(self.calls,0)
    def test_bad_usage_schema_and_missing_credential_fail_before_reservation(self):
        with sqlite3.connect(self.state/'experiment-budget.db') as db:db.execute('CREATE TABLE api_usage(wrong TEXT)')
        with self.assertRaisesRegex(ValueError,'usage_schema'):self.run_one()
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM api_attempts').fetchone()[0],1);db.execute('DROP TABLE api_usage')
        def missing():raise ValueError('credential_missing')
        with self.assertRaisesRegex(ValueError,'credential_missing'):e.once(self.state,self.s,authorized=True,secret=missing,clock=lambda:T)
        with sqlite3.connect(self.state/'experiment-budget.db') as db:self.assertEqual(db.execute('SELECT count(*) FROM api_attempts').fetchone()[0],1)
        self.assertEqual(self.calls,0)

    def test_test_budget_can_never_use_real_http_transport(self):
        (self.state/'.TEST_ONLY_BUDGET').write_text('test')
        with self.assertRaisesRegex(ValueError,'cannot_send_http'):e.once(self.state,self.s,authorized=True,clock=lambda:T)

    def test_invalid_model_response_is_retained_but_cannot_create_order(self):
        self.d['entry_level']='invented'
        result=self.run_one();self.assertFalse(result['admission']['allowed']);self.assertEqual(result['admission']['reason'],'unknown_level')
        self.assertEqual(self.run_one(),result)
        self.assertEqual(self.calls,1)
        with sqlite3.connect(self.state/'experiment-budget.db') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bracket_api_requests WHERE raw_response IS NOT NULL AND result IS NOT NULL').fetchone()[0],1)


if __name__=='__main__':unittest.main()
