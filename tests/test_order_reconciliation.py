import tempfile, unittest
from pathlib import Path
from trading_bot.order_journal import OrderJournal, Request, State, OfflineOrderCoordinator, SubmissionUncertain, DispatchBlocked, FaultPoint
from trading_bot.order_simulator import SimulatedExchange
from trading_bot.order_reconciliation import reconcile

class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.path=root/'journal.db'
        self.sim=SimulatedExchange(root/'exchange.db',scenario='timeout_after_accept')
        with OrderJournal(self.path) as j:
            self.intent=j.prepare('test','signal',Request('BUY',quote_quantity='10'))
            with self.assertRaises(SubmissionUncertain): OfflineOrderCoordinator(j,self.sim).dispatch(self.intent.intent_id)
    def test_repeat_and_no_resubmit(self):
        with OrderJournal(self.path) as j:
            obj,result=reconcile(j,self.sim,self.intent.intent_id)
            self.assertEqual((result,obj.state),('recorded',State.ACKNOWLEDGED))
            self.assertEqual(reconcile(j,self.sim,obj.intent_id)[1],'already_recorded')
            self.assertEqual(len(j.events(obj.intent_id)),4)
            with self.assertRaises(DispatchBlocked): OfflineOrderCoordinator(j,self.sim).dispatch(obj.intent_id)
        self.assertEqual(self.sim.counts()['submissions'],1)
    def test_unavailable_and_inconsistent(self):
        real=self.sim.query(self.intent.client_order_id)
        for answer in [None,{},dict(real,client_order_id='other'),dict(real,side='SELL'),dict(real,exchange_order_id=None),dict(real,status='REJECTED')]:
            self.sim.query=lambda _:answer
            with OrderJournal(self.path) as j:
                self.assertEqual(reconcile(j,self.sim,self.intent.intent_id)[1],'unresolved')
                self.assertEqual(j.get(self.intent.intent_id).state,State.UNKNOWN)
        def fail(_): raise TimeoutError('private')
        self.sim.query=fail
        with OrderJournal(self.path) as j:self.assertEqual(reconcile(j,self.sim,self.intent.intent_id)[1],'unresolved')
    def test_partial_is_recorded_not_settled(self):
        real=self.sim.query(self.intent.client_order_id); real['status']='PARTIALLY_FILLED'
        self.sim.query=lambda _:real
        with OrderJournal(self.path) as j:
            obj,outcome=reconcile(j,self.sim,self.intent.intent_id)
            self.assertEqual((outcome,obj.exchange_status),('recorded','PARTIALLY_FILLED'))
            with self.assertRaises(DispatchBlocked):OfflineOrderCoordinator(j,self.sim).dispatch(obj.intent_id)
    def test_crash_after_commit_does_not_duplicate(self):
        def fault(p):
            if p == FaultPoint.RESULT_COMMITTED: raise RuntimeError('crash after commit')
        with OrderJournal(self.path,fault=fault) as j:
            with self.assertRaises(RuntimeError):reconcile(j,self.sim,self.intent.intent_id)
        with OrderJournal(self.path) as j:
            self.assertEqual(reconcile(j,self.sim,self.intent.intent_id)[1],'already_recorded')
            self.assertEqual(len(j.events(self.intent.intent_id)),4)
            self.assertEqual(self.sim.counts()['submissions'],1)

    def test_transaction_fault_rolls_back_and_retry_is_idempotent(self):
        for point in (FaultPoint.RESULT_WRITTEN, FaultPoint.RESULT_BEFORE_COMMIT):
            def fault(p):
                if p == point: raise RuntimeError('crash')
            with OrderJournal(self.path,fault=fault) as j:
                with self.assertRaises(RuntimeError):reconcile(j,self.sim,self.intent.intent_id)
            with OrderJournal(self.path) as j:
                self.assertEqual(j.get(self.intent.intent_id).state,State.UNKNOWN)
                self.assertEqual(len(j.events(self.intent.intent_id)),3)
        with OrderJournal(self.path) as j:
            self.assertEqual(reconcile(j,self.sim,self.intent.intent_id)[1],'recorded')
            self.assertEqual(reconcile(j,self.sim,self.intent.intent_id)[1],'already_recorded')
            self.assertEqual(len(j.events(self.intent.intent_id)),4)
if __name__=='__main__': unittest.main()
