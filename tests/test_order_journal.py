import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from trading_bot.order_journal import (DispatchBlocked, FaultPoint, IntentConflict,
    JournalError, OfflineOrderCoordinator, OrderJournal, Receipt, Request, State, SubmissionUncertain)
from trading_bot.order_simulator import SimulatedExchange
from tools.order_fault_harness import run_matrix


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="order-journal-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/"intents.db"
        self.sim=SimulatedExchange(self.root/"simulator.db")
        self.request=Request("BUY",quote_quantity="10")

    def test_canonical_request_and_key_are_stable_after_reopen(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare("sim","signal-1",Request("BUY",quote_quantity="010.000"))
        with OrderJournal(self.path) as journal:
            replay=journal.prepare("sim","signal-1",self.request)
            self.assertEqual(replay,intent)
            self.assertEqual(replay.request.quote_quantity,"10")
            self.assertEqual(len(journal.events(intent.intent_id)),1)
            self.assertEqual(journal.db.execute("SELECT environment FROM order_intents").fetchone()[0],"simulation")

    def test_conflicting_request_never_reuses_business_key(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare("sim","signal-1",self.request)
            with self.assertRaises(IntentConflict):
                journal.prepare("sim","signal-1",Request("BUY",quote_quantity="11"))
            self.assertEqual(journal.get(intent.intent_id),intent)
            self.assertEqual(len(journal.events(intent.intent_id)),1)

    def test_invalid_amounts_and_out_of_scope_markets_rejected(self):
        for value in (0,1.5,"0","-1","NaN","Infinity","1e2","", "9"*19):
            with self.subTest(value=value),self.assertRaises(ValueError):
                Request("BUY",quote_quantity=value)
        for params in (dict(side="SELL",quote_quantity="10"),dict(side="BUY",quantity="1",quote_quantity="1"),
                       dict(side="BUY"),dict(side="BUY",quantity="1",market="futures"),dict(side="BUY",quantity="1",symbol="ETHUSDT")):
            with self.assertRaises(ValueError):Request(**params)

    def test_success_is_atomic_and_cannot_dispatch_again(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare("sim","signal-1",self.request)
            result=OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)
            self.assertEqual(result.state,State.ACKNOWLEDGED)
            self.assertEqual(result.exchange_order_id,"sim-1")
            self.assertEqual(result.exchange_status,"NEW")
            self.assertEqual([e['to_state'] for e in journal.events(intent.intent_id)],['PREPARED','SUBMITTING','ACKNOWLEDGED'])
            with self.assertRaises(DispatchBlocked):OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)
        self.assertEqual(self.sim.counts(),dict(submissions=1,accepted=1))

    def test_transaction_failure_rolls_back_intent_and_event(self):
        def fault(point):
            if point==FaultPoint.INTENT_BEFORE_COMMIT:raise RuntimeError('injected failure')
        with OrderJournal(self.path,fault=fault) as journal:
            with self.assertRaises(RuntimeError):journal.prepare("sim","signal-1",self.request)
            self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM order_intents').fetchone()[0],0)
            self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM order_events').fetchone()[0],0)
        self.assertEqual(self.sim.counts()['submissions'],0)

    def test_event_failure_rolls_back_state_and_prevents_submit(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare("sim","signal-1",self.request)
            journal.db.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON order_events WHEN NEW.to_state='SUBMITTING' BEGIN SELECT RAISE(ABORT,'test'); END")
            with self.assertRaises(sqlite3.IntegrityError):OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)
            self.assertEqual(journal.get(intent.intent_id).state,State.PREPARED)
            self.assertEqual(len(journal.events(intent.intent_id)),1)
        self.assertEqual(self.sim.counts()['submissions'],0)

    def test_result_storage_failure_blocks_resend(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare("sim","signal-1",self.request)
            journal.db.execute("CREATE TRIGGER fail_result BEFORE INSERT ON order_events WHEN NEW.to_state='ACKNOWLEDGED' BEGIN SELECT RAISE(ABORT,'test'); END")
            with self.assertRaises(sqlite3.IntegrityError):OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)
            self.assertEqual(journal.get(intent.intent_id).state,State.SUBMITTING)
            self.assertEqual(len(journal.events(intent.intent_id)),2)
        with OrderJournal(self.path) as journal:
            with self.assertRaises(DispatchBlocked):OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)
        self.assertEqual(self.sim.counts()['submissions'],1)

    def test_raw_error_body_is_not_persisted_or_exposed(self):
        class ErrorSimulator:
            is_simulator=True
            def submit(self,intent):raise TimeoutError('secret-sentinel https://example.invalid/private')
        with OrderJournal(self.path) as journal:
            intent=journal.prepare("sim","signal-1",self.request)
            with self.assertRaises(SubmissionUncertain) as error:
                OfflineOrderCoordinator(journal,ErrorSimulator()).dispatch(intent.intent_id)
            self.assertNotIn('secret-sentinel',str(error.exception))
            self.assertEqual(journal.get(intent.intent_id).state,State.UNKNOWN)
            self.assertNotIn('secret-sentinel','\n'.join(journal.db.iterdump()))
            self.assertTrue(error.exception.__suppress_context__)

    def test_unknown_and_rejected_cannot_be_reset_by_prepare(self):
        for scenario,state in [('timeout_after_accept',State.UNKNOWN),('reject',State.REJECTED)]:
            with self.subTest(scenario=scenario),OrderJournal(self.root/(scenario+'.db')) as journal:
                sim=SimulatedExchange(self.root/(scenario+'-exchange.db'),scenario=scenario)
                intent=journal.prepare('sim','signal-1',self.request)
                try:OfflineOrderCoordinator(journal,sim).dispatch(intent.intent_id)
                except SubmissionUncertain:pass
                replay=journal.prepare('sim','signal-1',self.request)
                self.assertEqual(replay.state,state)
                with self.assertRaises(DispatchBlocked):OfflineOrderCoordinator(journal,sim).dispatch(replay.intent_id)
                self.assertEqual(sim.counts()['submissions'],1)

    def test_events_append_only_and_illegal_transitions_rejected(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare('sim','signal-1',self.request)
            with self.assertRaises(DispatchBlocked):journal.record_receipt(intent.intent_id,Receipt('sim-1','NEW'))
            with self.assertRaises(DispatchBlocked):journal.mark_unknown(intent.intent_id)
            for sql in ('DELETE FROM order_events',"UPDATE order_events SET reason='SUBMIT_STARTED'"):
                with self.assertRaises(sqlite3.IntegrityError):journal.db.execute(sql)
            self.assertEqual(len(journal.events(intent.intent_id)),1)

    def test_foreign_bot_database_is_refused_without_migration(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE state(key TEXT,value TEXT)')
            db.execute("INSERT INTO state VALUES('sentinel','unchanged')");db.commit()
        with self.assertRaises(JournalError):OrderJournal(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM state').fetchall(),[('sentinel','unchanged')])
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('state',)])

    def test_unknown_schema_version_is_refused(self):
        with OrderJournal(self.path) as journal:journal.db.execute('UPDATE order_schema SET version=99')
        with self.assertRaises(JournalError):OrderJournal(self.path)

    def test_simulator_does_not_hide_duplicate_submissions(self):
        with OrderJournal(self.path) as journal:
            intent=journal.prepare('sim','signal-1',self.request)
            self.sim.submit(intent);self.sim.submit(intent)
        self.assertEqual(self.sim.counts(),dict(submissions=2,accepted=2))

    def test_simulator_and_journal_cannot_share_database(self):
        with OrderJournal(self.path):pass
        with self.assertRaises(ValueError):SimulatedExchange(self.path)
        with self.assertRaises(JournalError):OrderJournal(self.root/'simulator.db')

    def test_two_connections_cannot_claim_same_intent(self):
        barrier=Barrier(2)
        def compete():
            with OrderJournal(self.path) as journal:
                intent=journal.prepare('sim','signal-1',self.request)
                barrier.wait(timeout=5)
                try:
                    OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)
                    return True
                except DispatchBlocked:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:compete(),range(2)))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.sim.counts()['submissions'],1)

    def test_network_disabled_and_non_simulator_rejected(self):
        with patch('socket.socket',side_effect=AssertionError('network')),patch('socket.create_connection',side_effect=AssertionError('network')):
            with OrderJournal(self.path) as journal:
                with self.assertRaises(ValueError):OfflineOrderCoordinator(journal,object())
                intent=journal.prepare('sim','signal-1',self.request)
                OfflineOrderCoordinator(journal,self.sim).dispatch(intent.intent_id)

    def test_hard_crash_matrix_and_two_process_race(self):
        result=run_matrix()
        self.assertEqual(result['scenarios'],19)
        self.assertEqual(result['passed'],19)


if __name__=='__main__':unittest.main()
