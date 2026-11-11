import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from research.adaptive_operator_intent import create_entry, execute_entry, create_exit, execute_exit, reconcile_receipt
from research.adaptive_experiment_fence import install
from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_store import FuturesStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.unified_queue import UnifiedQueue


class OperatorIntentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queue.db'
        self.queue = UnifiedQueue(self.path)
        self.addCleanup(self.queue.close)

    def test_unique_explicit_test_intent_and_replay(self):
        args = dict(test_id='check-long-001', direction='LONG', fixed_ms=1000)
        a = create_entry(self.queue, **args)
        b = create_entry(self.queue, **args)
        self.assertFalse(a['replayed'])
        self.assertTrue(b['replayed'])
        self.assertEqual(a['job_id'], b['job_id'])
        row = self.queue.db.execute('SELECT payload FROM unified_jobs').fetchone()
        self.assertEqual(json.loads(row[0])['classification'], 'execution_test')
        self.assertEqual(self.queue.db.execute('SELECT COUNT(*) FROM unified_jobs').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'operator_action_identity_conflict'):
            create_entry(self.queue, **dict(args, direction='SHORT'))

    def test_concurrent_different_intents_cannot_both_be_pending(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def contender(identifier):
            q = UnifiedQueue(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    create_entry(q, test_id=identifier, direction='SHORT', fixed_ms=1000)
                    outcomes.append('created')
                except ValueError as exc:
                    outcomes.append(str(exc))
            finally:
                q.close()
        threads = [threading.Thread(target=contender, args=(f'check-short-{n}',)) for n in (1, 2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertCountEqual(outcomes, ['created', 'queued_action_exists'])
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM unified_jobs WHERE status='PENDING'").fetchone()[0], 1)

    def test_unfinished_other_action_blocks_test_intent(self):
        with self.queue.transaction():
            self.queue.db.execute("INSERT INTO unified_updates VALUES(1,'other','/pause','spot')")
            self.queue.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(1,'spot','/pause','{}')")
        with self.assertRaisesRegex(ValueError, 'queued_action_exists'):
            create_entry(self.queue, test_id='check-long-002', direction='LONG', fixed_ms=1000)

    def make_financial(self):
        root = self.path.parent
        s = RuntimeStore(root / 'spot', {'mode': 'paper', 'symbol': 'BTCUSDT'})
        s.close()
        f = FuturesStore(root / 'future', fee_bps='10', slippage_bps='5')
        f.close()
        financial = root / 'financial.db'
        consolidate_snapshots(root / 'spot', root / 'future', financial, captured_at=1)
        shared = SharedPaperStore(financial)
        CrossPaperBook(shared)
        MarginPaperBook(shared.ledger)
        install(shared.db)
        shared.close()
        store = FuturesStore(financial, shared=True, fee_bps='10', slippage_bps='5')
        store.command(1, True, '/resume')
        return store

    def test_real_paper_entry_both_directions_and_replay(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                with tempfile.TemporaryDirectory() as tmp:
                    oldpath, oldqueue = self.path, self.queue
                    self.path = Path(tmp) / 'queue.db'
                    self.queue = UnifiedQueue(self.path)
                    store = None
                    try:
                        store = self.make_financial()
                        job = create_entry(self.queue, test_id='check-'+direction.lower()+'-001', direction=direction, fixed_ms=1000)
                        info = spot_info('BTC')
                        info.update(contractType='PERPETUAL', marginAsset='USDT', marketTakeBound='0.03')
                        trade = {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':1001,'received_ms':1001}
                        mark = {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':1001,'received_ms':1001,'next_due_ms':999999}
                        kwargs = dict(job_id=job['job_id'],trade=trade,mark=mark,filters=info,filters_ms=1001,next_funding_ms=999999,clock=lambda:1001)
                        def crash(stage):
                            if stage == 'after_open': raise RuntimeError('lost acknowledgement')
                        with self.assertRaisesRegex(RuntimeError, 'lost acknowledgement'):
                            execute_entry(store, self.path, **kwargs, fault=crash)
                        store.close()
                        self.queue.close()
                        self.queue = UnifiedQueue(self.path)
                        store = FuturesStore(self.path.parent / 'financial.db', shared=True, fee_bps='10', slippage_bps='5')
                        result = execute_entry(store, self.path, **kwargs)
                        self.assertEqual(result['status'], 'filled_existing')
                        self.assertEqual(result['position']['direction'], direction)
                        with self.queue.transaction():
                            self.queue.db.execute("UPDATE unified_jobs SET status='PENDING',result=NULL WHERE id=?",(job['job_id'],))
                        self.assertEqual(execute_entry(store, self.path, **kwargs)['status'], 'reconciliation_required')
                        self.assertEqual(reconcile_receipt(store, self.path, job_id=job['job_id'])['status'], 'reconciled')
                        self.assertEqual(execute_entry(store, self.path, **kwargs)['status'], 'filled_existing')
                        self.assertEqual(execute_entry(store, self.path, **dict(kwargs,clock=lambda:7002))['status'], 'filled_existing')
                        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM futures_events WHERE event='OPEN'").fetchone()[0], 1)
                        competing=create_entry(self.queue,test_id='competing-test-001',direction='SHORT',fixed_ms=1000)
                        with self.assertRaisesRegex(ValueError,'position_exists'):
                            execute_entry(store,self.path,**dict(kwargs,job_id=competing['job_id']))
                        self.queue.acknowledge(competing['job_id'],'conflicting exposure',applied=False,notify=False)
                        exit_job = create_exit(self.queue, test_id='check-'+direction.lower()+'-001', position_id=result['position']['id'], fixed_ms=1001)
                        exit_kwargs = dict(job_id=exit_job['job_id'],trade=trade,mark=mark,filters=info,filters_ms=1001,clock=lambda:1001)
                        with self.assertRaisesRegex(ValueError, 'trade_stale'):
                            execute_exit(store, self.path, **dict(exit_kwargs, clock=lambda:7002))
                        self.assertIsNotNone(store.get('position'))
                        def close_crash(stage):
                            if stage == 'after_close': raise RuntimeError('lost close acknowledgement')
                        with self.assertRaisesRegex(RuntimeError, 'lost close acknowledgement'):
                            execute_exit(store, self.path, **exit_kwargs, fault=close_crash)
                        store.close()
                        self.queue.close()
                        self.queue = UnifiedQueue(self.path)
                        store = FuturesStore(self.path.parent / 'financial.db', shared=True, fee_bps='10', slippage_bps='5')
                        closed = execute_exit(store, self.path, **exit_kwargs)
                        self.assertEqual(closed['status'], 'filled_existing')
                        self.assertEqual(execute_exit(store, self.path, **dict(exit_kwargs,clock=lambda:7002))['status'], 'filled_existing')
                        self.assertEqual(execute_exit(store, self.path, **exit_kwargs)['status'], 'filled_existing')
                        self.assertIsNone(store.get('position'))
                        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0], 1)
                        self.assertTrue(store.shared.ledger.reconcile()['balanced'])
                    finally:
                        if store is not None: store.close()
                        self.queue.close()
                        self.path, self.queue = oldpath, oldqueue

    def test_stale_price_and_unproven_funding_leave_position_empty(self):
        store = self.make_financial()
        try:
            job = create_entry(self.queue, test_id='stale-check-001', direction='LONG', fixed_ms=1000)
            info = spot_info('BTC')
            info.update(contractType='PERPETUAL', marginAsset='USDT', marketTakeBound='0.03')
            trade = {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':1001,'received_ms':1001}
            mark = {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':1001,'received_ms':1001,'next_due_ms':999999}
            kwargs = dict(job_id=job['job_id'],trade=trade,mark=mark,filters=info,filters_ms=1001,next_funding_ms=999999,clock=lambda:7002)
            with self.assertRaisesRegex(ValueError, 'trade_stale'):
                execute_entry(store, self.path, **kwargs)
            kwargs['clock'] = lambda:1001
            kwargs['next_funding_ms'] = 888888
            with self.assertRaisesRegex(ValueError, 'funding_schedule_unavailable'):
                execute_entry(store, self.path, **kwargs)
            self.assertIsNone(store.get('position'))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM futures_events WHERE event='OPEN'").fetchone()[0], 0)
            self.assertTrue(store.shared.ledger.reconcile()['balanced'])
        finally:
            store.close()

    def test_interruption_before_financial_commit_rolls_back(self):
        store=self.make_financial()
        try:
            job=create_entry(self.queue,test_id='before-commit-001',direction='SHORT',fixed_ms=1000)
            info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
            trade={'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':1001,'received_ms':1001}
            mark={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':1001,'received_ms':1001,'next_due_ms':999999}
            def fail(point):
                if point=='before_commit':raise RuntimeError('interrupted before commit')
            store.fault=fail
            with self.assertRaisesRegex(RuntimeError,'interrupted before commit'):
                execute_entry(store,self.path,job_id=job['job_id'],trade=trade,mark=mark,filters=info,filters_ms=1001,next_funding_ms=999999,clock=lambda:1001)
            store.fault=lambda point:None
            self.assertIsNone(store.get('position'))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],0)
            self.assertEqual(self.queue.db.execute('SELECT status FROM unified_jobs').fetchone()[0],'PENDING')
            self.assertTrue(store.shared.ledger.reconcile()['balanced'])
        finally:store.close()

    def test_worker_drives_both_directions_and_replays_complete(self):
        from research.adaptive_operator_worker import step
        store=self.make_financial();store.close()
        info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
        class Feed:
            @staticmethod
            def futures_quote():return {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':1001,'received_ms':1001,'next_due_ms':999999}
            @staticmethod
            def futures_execution_trade(*,not_before_ms):return {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':1001,'received_ms':1001}
            @staticmethod
            def filters(market):return {'filters':info,'received_ms':1001}
        for direction in ('LONG','SHORT'):
            args=dict(test_id='worker-'+direction.lower()+'-001',direction=direction,clock=lambda:1001,provider=Feed)
            self.assertEqual(step(self.path.parent,**args)['status'],'OPEN')
            self.assertEqual(step(self.path.parent,**args)['status'],'CLOSED')
            self.assertEqual(step(self.path.parent,**args)['status'],'COMPLETE')
            self.assertEqual(step(self.path.parent,**args)['status'],'COMPLETE')

    def test_worker_recovers_from_interruption_after_entry(self):
        from research.adaptive_operator_worker import step
        store=self.make_financial();store.close()
        info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
        class Feed:
            @staticmethod
            def futures_quote():return {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':1001,'received_ms':1001,'next_due_ms':999999}
            @staticmethod
            def futures_execution_trade(*,not_before_ms):return {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':1001,'received_ms':1001}
            @staticmethod
            def filters(market):return {'filters':info,'received_ms':1001}
        def crash(stage):
            if stage=='after_open':raise RuntimeError('worker terminated')
        args=dict(test_id='worker-recover-001',direction='LONG',clock=lambda:1001,provider=Feed)
        with self.assertRaisesRegex(RuntimeError,'worker terminated'):step(self.path.parent,**args,fault=crash)
        self.assertEqual(step(self.path.parent,**args)['status'],'CLOSED')
        result=step(self.path.parent,**args);self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(result['close']['reason'],'TEST')

    @unittest.skipUnless(hasattr(os,'fork'),'POSIX process semantics required')
    def test_real_process_death_after_entry_restarts_without_duplicate(self):
        from research.adaptive_operator_worker import step
        store=self.make_financial();store.close()
        info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
        class Feed:
            @staticmethod
            def futures_quote():return {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':1001,'received_ms':1001,'next_due_ms':999999}
            @staticmethod
            def futures_execution_trade(*,not_before_ms):return {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':1001,'received_ms':1001}
            @staticmethod
            def filters(market):return {'filters':info,'received_ms':1001}
        args=dict(test_id='worker-kill-001',direction='SHORT',clock=lambda:1001,provider=Feed)
        pid=os.fork()
        if pid==0:
            def kill(stage):
                if stage=='after_open':os._exit(79)
            step(self.path.parent,**args,fault=kill)
            os._exit(1)
        _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),79)
        pid=os.fork()
        if pid==0:
            def kill_close(stage):
                if stage=='after_close':os._exit(80)
            step(self.path.parent,**args,fault=kill_close)
            os._exit(1)
        _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),80)
        self.assertEqual(step(self.path.parent,**args)['status'],'COMPLETE')
        store=FuturesStore(self.path.parent/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
        try:
            self.assertIsNone(store.get('position'))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0],1)
            self.assertTrue(store.shared.ledger.reconcile()['balanced'])
        finally:store.close()


if __name__ == '__main__':
    unittest.main()
