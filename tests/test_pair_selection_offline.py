import concurrent.futures
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_pair_selection_offline import select_spot_pair_offline
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.unified_queue import UnifiedQueue


class PairSelectionOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        spot=RuntimeStore(root/'spot.db',{'mode':'paper','symbol':'BTCUSDT'})
        futures=FuturesStore(root/'futures.db');spot.close();futures.close()
        self.financial=root/'shared.db';self.queue=root/'queue.db'
        consolidate_snapshots(root/'spot.db',root/'futures.db',self.financial,captured_at=1)
        q=UnifiedQueue(self.queue);q.close()
        self.shared=SharedPaperStore(self.financial);self.addCleanup(self.shared.close)

    def choose(self,**overrides):
        args=dict(request_id='selection:1',expected_symbol='BTCUSDT',expected_version=0,
                  symbol='SOLUSDT',metadata=spot_info(),price='100',quantity='0.1',
                  observed_at=100,clock=lambda:101)
        args.update(overrides)
        return select_spot_pair_offline(self.shared,self.queue,**args)

    def test_selection_preserves_capital_and_is_idempotent_across_reopen(self):
        before=self.shared.snapshot('spot');events=self.shared.db.execute('SELECT count(*) FROM ledger_events').fetchone()[0]
        result=self.choose();self.assertEqual(result['symbol'],'SOLUSDT');self.assertFalse(result['replayed'])
        self.assertTrue(self.choose()['replayed'])
        after=self.shared.snapshot('spot')
        self.assertEqual((before['cash'],before['pnl'],before['position']),(after['cash'],after['pnl'],after['position']))
        self.assertTrue(after['paused']);self.assertEqual(events,self.shared.db.execute('SELECT count(*) FROM ledger_events').fetchone()[0])
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable pair selection'):
            self.shared.db.execute("UPDATE pair_spot_selection_events SET version=9 WHERE request_id='selection:1'")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable pair selection'):
            self.shared.db.execute("DELETE FROM pair_spot_selection_events WHERE request_id='selection:1'")
        self.shared.close();self.shared=SharedPaperStore(self.financial);self.addCleanup(self.shared.close)
        self.assertTrue(self.choose()['replayed'])
        with self.assertRaisesRegex(ValueError,'Conflicting request'):
            self.choose(symbol='ETHUSDT',metadata=spot_info('ETH'))

    def test_stale_data_version_and_pending_command_fail_without_change(self):
        with self.assertRaisesRegex(ValueError,'stale or future'):
            self.choose(observed_at=0,clock=lambda:200)
        with self.assertRaisesRegex(ValueError,'Stale pair'):
            self.choose(expected_version=1)
        queue=UnifiedQueue(self.queue)
        try:queue.receive(1,True,'/pause')
        finally:queue.close()
        with self.assertRaisesRegex(ValueError,'command pending'):
            self.choose()
        self.assertEqual(self.shared._get('spot','identity')['symbol'],'BTCUSDT')

    def test_after_waiting_on_real_database_lock_data_must_still_be_fresh(self):
        observed=time.time()
        with sqlite3.connect(self.financial,isolation_level=None) as lock:
            lock.execute('BEGIN IMMEDIATE')
            def worker():
                shared=SharedPaperStore(self.financial)
                try:
                    return select_spot_pair_offline(shared,self.queue,request_id='wait',
                        expected_symbol='BTCUSDT',expected_version=0,symbol='SOLUSDT',
                        metadata=spot_info(),price='100',quantity='0.1',observed_at=observed,max_age=.15)
                finally:shared.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(worker);time.sleep(.25);self.assertFalse(future.done());lock.commit()
                with self.assertRaisesRegex(ValueError,'stale or future'):future.result(timeout=5)
        self.assertEqual(self.shared._get('spot','identity')['symbol'],'BTCUSDT')

    def test_concurrent_selection_uses_version_and_single_result(self):
        def worker(request,symbol):
            shared=SharedPaperStore(self.financial)
            try:
                return select_spot_pair_offline(shared,self.queue,request_id=request,
                    expected_symbol='BTCUSDT',expected_version=0,symbol=symbol,
                    metadata=spot_info(symbol[:-4]),price='100',quantity='.1',observed_at=100,clock=lambda:101)
            finally:shared.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(worker,'one','SOLUSDT'),pool.submit(worker,'two','ETHUSDT')]
            results=[];errors=[]
            for future in futures:
                try:results.append(future.result(timeout=10))
                except ValueError as exc:errors.append(str(exc))
        self.assertEqual(len(results),1);self.assertEqual(len(errors),1)
        self.assertIn('Stale pair',errors[0])
        self.assertEqual(self.shared._get('spot','pair_selection_version'),1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_process_death_around_commit_is_replay_safe(self):
        script='''import os,sys\nfrom tests.test_paper_pair_catalog import spot_info\nfrom trading_bot.paper_shared_store import SharedPaperStore\nfrom trading_bot.paper_pair_selection_offline import select_spot_pair_offline\ns=SharedPaperStore(sys.argv[1])\ndef fault(stage):\n if stage==sys.argv[3]:os._exit(74)\ns.fault=fault\nselect_spot_pair_offline(s,sys.argv[2],request_id="crash",expected_symbol="BTCUSDT",expected_version=0,symbol="SOLUSDT",metadata=spot_info(),price="100",quantity=".1",observed_at=100,clock=lambda:101)\n'''
        for stage in ('before_commit','after_commit'):
            with self.subTest(stage=stage):
                child=subprocess.run([sys.executable,'-B','-c',script,str(self.financial),str(self.queue),stage],timeout=20,capture_output=True)
                self.assertEqual(child.returncode,74,child.stderr.decode())
                current=self.shared._get('spot','identity')['symbol']
                self.assertEqual(current,'SOLUSDT' if stage=='after_commit' else 'BTCUSDT')
        result=self.choose(request_id='crash')
        self.assertTrue(result['replayed']);self.assertEqual(self.shared._get('spot','pair_selection_version'),1)

    def test_open_position_is_never_relabelled_or_closed_by_selection(self):
        store=RuntimeStore(self.financial,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
        try:
            with store.transaction():store.set('paused',False)
            client=store.prepare('BUY',Decimal('.1'),Decimal('100'),'fixture-buy','fixture')
            store.claim(client)
            row=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(client,)).fetchone()
            store.apply(client,PaperExchange.inspect_saved(row))
            with store.transaction():store.set('paused',True)
            before=store.get('position')
            with self.assertRaisesRegex(ValueError,'position or unknown fees'):
                self.choose()
            self.assertEqual(store.get('position'),before)
            self.assertEqual(store.get('identity')['symbol'],'BTCUSDT')
            self.assertTrue(store.shared.ledger.reconcile()['balanced'])
        finally:store.close()

    def test_sol_shared_fill_survives_process_death_at_commit_boundaries(self):
        self.choose()
        identity={'mode':'paper','symbol':'SOLUSDT'}
        store=RuntimeStore(self.financial,identity,shared=True)
        try:
            with store.transaction():store.set('paused',False)
            client=store.prepare('BUY',Decimal('.1'),Decimal('100'),'sol-crash','fixture')
            store.claim(client)
        finally:store.close()
        script='''import os,sys\nfrom trading_bot.runtime_store import RuntimeStore\nfrom trading_bot.runtime_exchange import PaperExchange\ns=RuntimeStore(sys.argv[1],{"mode":"paper","symbol":"SOLUSDT"},shared=True)\nr=s.db.execute("SELECT * FROM runtime_orders WHERE client_id=?",(sys.argv[2],)).fetchone()\nreport=PaperExchange.inspect_saved({**dict(r),"symbol":"SOLUSDT"})\ndef fault(stage):\n if stage==sys.argv[3]:os._exit(74)\ns.fault=fault\ns.apply(sys.argv[2],report)\n'''
        for stage in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.financial),client,stage],timeout=20,capture_output=True)
            self.assertEqual(child.returncode,74,child.stderr.decode())
            check=RuntimeStore(self.financial,identity,shared=True)
            try:
                qty=Decimal(check.get('position')['quantity'])
                self.assertEqual(qty,Decimal('.1') if stage=='after_commit' else Decimal(0))
                self.assertEqual(check.shared.ledger.balance('spot:position','SOL'),qty)
                self.assertTrue(check.shared.ledger.reconcile()['balanced'])
            finally:check.close()
        check=RuntimeStore(self.financial,identity,shared=True)
        try:
            row=check.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(client,)).fetchone()
            report=PaperExchange.inspect_saved({**dict(row),'symbol':'SOLUSDT'})
            check.apply(client,report)
            self.assertEqual(check.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],1)
            self.assertEqual(check.shared.ledger.balance('spot:position','SOL'),Decimal('.1'))
        finally:check.close()
