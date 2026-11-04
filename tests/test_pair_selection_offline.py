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
from trading_bot.paper_pair_selection_offline import (select_spot_pair_offline, select_isolated_pair_offline,
                                                      select_margin_pairs_offline)
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_paper_strategy import MarginPaperStrategy
from trading_bot.paper_pair_worker_offline import process_spot_pair, process_isolated_pair, process_margin_pair
from trading_bot.paper_pair_runtime_offline import (restore_margin_book, ExperimentalMarginRunner,
                                                    restore_spot_store, restore_isolated_store,
                                                    ExperimentalSpotRunner, ExperimentalIsolatedRunner)
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report
from trading_bot.runtime_store import RuntimeStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.app import Application, TelegramTransport
from unittest.mock import patch


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

    def confirmed_pair_job(self):
        q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        try:
            q.receive(1,True,'/pair SOLUSDT',now=100,pairs={'spot':{'symbols':['BTCUSDT'],'version':0}})
            token=q.db.execute('SELECT id FROM unified_pair_proposals').fetchone()[0]
            q.receive(2,True,'/confirm '+token,now=101)
            return token,q.pending('spot')[0]['id']
        finally:q.close()

    def test_telegram_poll_captures_financial_snapshot_at_receipt(self):
        now=time.time();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        store=RuntimeStore(self.financial,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
        try:
            update={'update_id':1,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},
                                             'date':int(now),'text':'/pair SOLUSDT'}}
            telegram=TelegramTransport('FAKE','1',lambda req:{'ok':True,'result':[update]},started=now-1)
            telegram.poll(store,q)
            proposal=q.db.execute('SELECT * FROM unified_pair_proposals').fetchone()
            self.assertEqual(proposal['market'],'spot')
            self.assertEqual(proposal['version'],0)
            self.assertEqual(proposal['previous'],'["BTCUSDT"]')
            self.assertFalse(store.db.in_transaction)
        finally:store.close();q.close()

    def test_confirmed_pair_job_executes_once_and_preserves_capital(self):
        token,job=self.confirmed_pair_job();before=self.shared.snapshot('spot')
        result=self.choose(request_id='pair:'+token,confirm_job_id=job,clock=lambda:102)
        self.assertEqual(result['symbol'],'SOLUSDT')
        self.assertTrue(self.choose(request_id='pair:'+token,confirm_job_id=job,clock=lambda:300)['replayed'])
        after=self.shared.snapshot('spot')
        self.assertEqual((before['cash'],before['pnl'],before['position']),
                         (after['cash'],after['pnl'],after['position']))
        self.assertTrue(after['paused']);self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_old_spot_application_cannot_submit_after_pair_change(self):
        store=RuntimeStore(self.financial,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
        try:
            app=Application(store,PaperExchange(),None)
            self.choose()
            with self.assertRaisesRegex(Exception,'pair version superseded'):
                app.execute('BUY',Decimal('.05'),Decimal('100'),'stale:buy','SIGNAL',quote_at=time.time())
            self.assertEqual(store.db.execute('SELECT count(*) FROM runtime_orders').fetchone()[0],0)
            with self.assertRaisesRegex(Exception,'pair version superseded'):app.tick()
        finally:store.close()

    def test_old_isolated_store_cannot_write_after_pair_change(self):
        old=FuturesStore(self.financial,shared=True)
        info=spot_info();info.update(contractType='PERPETUAL',marginAsset='USDT')
        try:
            select_isolated_pair_offline(self.shared,self.queue,request_id='isolated:choose',
                expected_symbol='BTCUSDT',expected_version=0,symbol='SOLUSDT',metadata=info,
                price='100',quantity='.1',observed_at=100,clock=lambda:101)
            with self.assertRaisesRegex(Exception,'pair version superseded'):
                old.open_position('LONG',Decimal('100'),100000,'stale:entry')
            self.assertIsNone(self.shared._get('futures','position'))
        finally:old.close()

    def test_spot_and_isolated_pair_identity_restored_without_new_capital(self):
        before_spot=self.shared.snapshot('spot')['cash'];before_future=self.shared.snapshot('futures')['cash']
        self.choose()
        info=spot_info();info.update(contractType='PERPETUAL',marginAsset='USDT')
        select_isolated_pair_offline(self.shared,self.queue,request_id='isolated:choose',
            expected_symbol='BTCUSDT',expected_version=0,symbol='SOLUSDT',metadata=info,
            price='100',quantity='.1',observed_at=100,clock=lambda:101)
        spot=restore_spot_store(self.financial);future=restore_isolated_store(self.financial)
        try:
            self.assertEqual(spot.get('identity')['symbol'],'SOLUSDT')
            self.assertEqual(future.get('identity')['symbol'],'SOLUSDT')
            self.assertEqual(spot.get('cash'),before_spot);self.assertEqual(future.get('cash'),before_future)
            self.assertTrue(spot.get('paused'));self.assertTrue(future.get('paused'))
        finally:spot.close();future.close()

    def test_same_symbol_new_generation_fences_old_spot_and_isolated_writers(self):
        spot=RuntimeStore(self.financial,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
        future=FuturesStore(self.financial,shared=True)
        try:
            app=Application(spot,PaperExchange(),None)
            self.choose(symbol='BTCUSDT',metadata=spot_info('BTC'))
            info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT')
            select_isolated_pair_offline(self.shared,self.queue,request_id='same:future',
                expected_symbol='BTCUSDT',expected_version=0,symbol='BTCUSDT',metadata=info,
                price='100',quantity='.1',observed_at=100,clock=lambda:101)
            with self.assertRaisesRegex(Exception,'pair version superseded'):
                app.execute('BUY',Decimal('.05'),Decimal('100'),'same:spot','SIGNAL',quote_at=time.time())
            with self.assertRaisesRegex(Exception,'pair version superseded'):
                future.open_position('LONG',Decimal('100'),100000,'same:future')
        finally:spot.close();future.close()

    def test_telegram_to_spot_worker_crash_after_commit_replays_once(self):
        token,job=self.confirmed_pair_job();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        class Reader:
            def get(inner,path,params):
                self.assertFalse(self.shared.db.in_transaction);self.assertFalse(q.db.in_transaction)
                if path.endswith('exchangeInfo'):return {'symbols':[spot_info()]}
                return {'symbol':'SOLUSDT','price':'100'}
        def crash(stage):
            if stage=='after_financial_commit':raise RuntimeError('simulated interruption')
        try:
            before=self.shared.snapshot('spot')['cash']
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                process_spot_pair(q,self.shared,Reader(),clock=lambda:102,fault=crash)
            self.assertEqual(len(q.pending('spot')),1)
            result=process_spot_pair(q,self.shared,Reader(),clock=lambda:103)
            self.assertTrue(result['replayed']);self.assertEqual(q.pending('spot'),[])
            self.assertEqual(self.shared.snapshot('spot')['cash'],before)
            self.assertTrue(self.shared.snapshot('spot')['paused'])
            self.assertEqual(self.shared.db.execute('SELECT count(*) FROM pair_spot_selection_events').fetchone()[0],1)
        finally:q.close()

    def test_expired_pair_job_rejected_without_blocking_following_pause(self):
        token,job=self.confirmed_pair_job();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        q.receive(3,True,'/pause',now=221)
        class Reader:
            def get(inner,path,params):
                return {'symbols':[spot_info()]} if path.endswith('exchangeInfo') else {'symbol':'SOLUSDT','price':'100'}
        try:
            self.assertIsNone(process_spot_pair(q,self.shared,Reader(),clock=lambda:221))
            self.assertEqual(q.db.execute('SELECT status FROM unified_jobs WHERE id=?',(job,)).fetchone()[0],'FAILED')
            self.assertEqual([r['command'] for r in q.pending('spot')],['/pause'])
            self.assertEqual(self.shared._get('spot','identity')['symbol'],'BTCUSDT')
        finally:q.close()

    def test_pair_worker_temporary_data_error_does_not_discard_job(self):
        token,job=self.confirmed_pair_job();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        class Reader:
            def get(inner,path,params):raise TimeoutError('fixture')
        try:
            with self.assertRaises(TimeoutError):process_spot_pair(q,self.shared,Reader(),clock=lambda:102)
            self.assertEqual(q.db.execute('SELECT status FROM unified_jobs WHERE id=?',(job,)).fetchone()[0],'PENDING')
            self.assertEqual(self.shared._get('spot','identity')['symbol'],'BTCUSDT')
        finally:q.close()

    def test_pair_worker_invalid_filter_is_final_and_does_not_block_queue(self):
        token,job=self.confirmed_pair_job();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        q.receive(3,True,'/pause',now=102)
        class Reader:
            def get(inner,path,params):
                if path.endswith('exchangeInfo'):
                    info=spot_info();info['status']='BREAK';return {'symbols':[info]}
                return {'symbol':'SOLUSDT','price':'100'}
        try:
            self.assertIsNone(process_spot_pair(q,self.shared,Reader(),clock=lambda:102))
            self.assertEqual(q.db.execute('SELECT status FROM unified_jobs WHERE id=?',(job,)).fetchone()[0],'FAILED')
            self.assertEqual([r['command'] for r in q.pending('spot')],['/pause'])
        finally:q.close()

    def test_spot_runner_rebinds_after_financial_commit_before_ack(self):
        self.confirmed_pair_job();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        store=RuntimeStore(self.financial,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
        class Reader:
            def get(inner,path,params):
                return {'symbols':[spot_info()]} if path.endswith('exchangeInfo') else {'symbol':'SOLUSDT','price':'100'}
        runner=ExperimentalSpotRunner(store,q,Reader());old=runner.app
        def crash(stage):
            if stage=='after_financial_commit':raise RuntimeError('fixture crash')
        try:
            with self.assertRaises(RuntimeError):runner.pair_step(clock=lambda:102,fault=crash)
            self.assertEqual(runner.app.symbol,'SOLUSDT')
            with self.assertRaisesRegex(Exception,'version superseded'):old.tick()
            self.assertTrue(runner.pair_step(clock=lambda:103)['replayed'])
            self.assertTrue(store.get('paused'));self.assertEqual(q.pending('spot'),[])
        finally:store.close();q.close()

    def test_isolated_runner_rebinds_after_financial_commit_before_ack(self):
        q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        q.receive(1,True,'/mode futures',now=100)
        q.receive(2,True,'/pair SOLUSDT',now=100,pairs={'futures':{'symbols':['BTCUSDT'],'version':0}})
        token=q.db.execute('SELECT id FROM unified_pair_proposals').fetchone()[0]
        q.receive(3,True,'/confirm '+token,now=101)
        store=FuturesStore(self.financial,shared=True)
        class Reader:
            def __init__(inner,symbol):inner.pair_symbol=symbol
            def get(inner,path):
                info=spot_info();info.update(contractType='PERPETUAL',marginAsset='USDT')
                return {'symbols':[info]}
            def mark(inner):return Decimal('100'),102000
        runner=ExperimentalIsolatedRunner(store,q,Reader)
        def crash(stage):
            if stage=='after_financial_commit':raise RuntimeError('fixture crash')
        try:
            with self.assertRaises(RuntimeError):runner.pair_step(clock=lambda:102,fault=crash)
            self.assertEqual(runner.store.symbol,'SOLUSDT')
            self.assertTrue(runner.pair_step(clock=lambda:103)['replayed'])
            self.assertTrue(runner.store.get('paused'));self.assertEqual(q.pending('futures'),[])
        finally:runner.store.close();q.close()

    def test_isolated_pair_worker_replays_after_commit_without_capital_change(self):
        q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        q.receive(1,True,'/mode futures',now=100)
        q.receive(2,True,'/pair SOLUSDT',now=100,pairs={'futures':{'symbols':['BTCUSDT'],'version':0}})
        token=q.db.execute('SELECT id FROM unified_pair_proposals').fetchone()[0]
        q.receive(3,True,'/confirm '+token,now=101)
        class Reader:
            pair_symbol='SOLUSDT'
            def get(inner,path):
                self.assertFalse(self.shared.db.in_transaction);self.assertFalse(q.db.in_transaction)
                info=spot_info();info.update(contractType='PERPETUAL',marginAsset='USDT')
                return {'symbols':[info]}
            def mark(inner):return Decimal('100'),102000
        def crash(stage):
            if stage=='after_financial_commit':raise RuntimeError('simulated interruption')
        try:
            before=self.shared.snapshot('futures')['cash']
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                process_isolated_pair(q,self.shared,lambda symbol:Reader(),clock=lambda:102,fault=crash)
            result=process_isolated_pair(q,self.shared,lambda symbol:Reader(),clock=lambda:103)
            self.assertTrue(result['replayed']);self.assertEqual(q.pending('futures'),[])
            self.assertEqual(self.shared.snapshot('futures')['cash'],before)
            self.assertTrue(self.shared.snapshot('futures')['paused'])
        finally:q.close()

    def test_confirmed_pair_expiry_and_identity_are_rechecked_at_execution(self):
        token,job=self.confirmed_pair_job()
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            self.choose(request_id='pair:other',confirm_job_id=job,clock=lambda:102)
        with self.assertRaisesRegex(ValueError,'expired'):
            self.choose(request_id='pair:'+token,confirm_job_id=job,observed_at=220,clock=lambda:220)
        self.assertEqual(self.shared._get('spot','identity')['symbol'],'BTCUSDT')
        self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_confirmed_pair_expiry_after_waiting_for_sqlite_lock(self):
        now=time.time();q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        try:
            q.receive(1,True,'/pair SOLUSDT',now=now-119.5,pairs={'spot':{'symbols':['BTCUSDT'],'version':0}})
            token=q.db.execute('SELECT id FROM unified_pair_proposals').fetchone()[0]
            q.receive(2,True,'/confirm '+token,now=now)
            job=q.pending('spot')[0]['id']
        finally:q.close()
        with sqlite3.connect(self.financial,isolation_level=None) as lock:
            lock.execute('BEGIN IMMEDIATE')
            def worker():
                shared=SharedPaperStore(self.financial)
                try:
                    return select_spot_pair_offline(shared,self.queue,request_id='pair:'+token,
                        expected_symbol='BTCUSDT',expected_version=0,symbol='SOLUSDT',
                        metadata=spot_info(),price='100',quantity='.1',observed_at=now,confirm_job_id=job)
                finally:shared.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(worker);time.sleep(.8);self.assertFalse(future.done());lock.commit()
                with self.assertRaisesRegex(ValueError,'expired'):future.result(timeout=5)
        self.assertEqual(self.shared._get('spot','identity')['symbol'],'BTCUSDT')

    def test_margin_selection_preserves_old_asset_and_replays(self):
        MarginPaperBook(self.shared.ledger)
        stale=restore_margin_book(self.shared)
        with self.shared.transaction():
            self.shared.ledger._post('fixture:eth-dust','FIXTURE',{},
                [('margin_cross:free','ETH',Decimal('.000001')),('external:fixture','ETH',Decimal('-.000001'))])
        args=dict(request_id='margin:choose',expected_symbols=['BTCUSDT','ETHUSDT'],expected_version=0,
                  symbols=['BTCUSDT','SOLUSDT'],metadata={'BTCUSDT':spot_info('BTC'),'SOLUSDT':spot_info('SOL')},
                  prices={'BTCUSDT':'100','SOLUSDT':'100'},quantities={'BTCUSDT':'.1','SOLUSDT':'.1'},
                  observed_at=100,clock=lambda:101)
        before=self.shared.ledger.balance('margin_cross:free','ETH')
        result=select_margin_pairs_offline(self.shared,self.queue,**args)
        self.assertEqual(result['symbols'],['BTCUSDT','SOLUSDT'])
        with self.assertRaisesRegex(ValueError,'version superseded'):stale._check()
        self.assertTrue(select_margin_pairs_offline(self.shared,self.queue,**args)['replayed'])
        self.assertEqual(self.shared.ledger.balance('margin_cross:free','ETH'),before)
        self.assertEqual(self.shared.ledger.balance('margin_cross:free','SOL'),0)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        self.shared.close();self.shared=SharedPaperStore(self.financial);self.addCleanup(self.shared.close)
        book=restore_margin_book(self.shared)
        self.assertEqual(book.tradable,frozenset(('BTCUSDT','SOLUSDT')))
        self.assertIn('ETHUSDT',book.pairs)
        self.assertEqual(self.shared.ledger.balance('margin_cross:free','ETH'),before)
        with self.assertRaisesRegex(ValueError,'Invalid margin entry'):
            book.open('old','old-pos','ETHUSDT','LONG','.1','5',2,{},now_ms=101000)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)

    def test_margin_report_prices_third_asset_without_omitting_historical_dust(self):
        book=MarginPaperBook(self.shared.ledger,experimental_pairs={'SOLUSDT':'SOL'})
        with self.shared.transaction():
            self.shared.ledger._post('fixture:assets','FIXTURE',{},[
                ('margin_cross:free','SOL',Decimal('1')),('external:fixture','SOL',Decimal('-1')),
                ('margin_cross:free','ETH',Decimal('.001')),('external:fixture','ETH',Decimal('-.001'))])
        f={'lot_rules':[{'min':'0','max':'0','step':'.001'}],'min_notional':'0','max_notional':'0','observed_ms':1000}
        quotes={s:{'price':p,'time_ms':1000,'server_ms':1000,'source':'fixture',
                   'price_kind':'last_public_trade','filters':f} for s,p in
                [('BTCUSDT','100'),('ETHUSDT','200'),('SOLUSDT','50')]}
        book.record_quotes(quotes,now_ms=1000)
        report=live_report(self.shared,now=1)
        self.assertEqual(report['margin_cross']['asset_value'],Decimal('50.2'))
        self.assertEqual(report['margin_cross']['assets']['SOL']['free'],Decimal('1'))
        self.assertEqual(report['margin_cross']['assets']['ETH']['free'],Decimal('.001'))
        self.assertIsNotNone(report['total']);self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_margin_selection_rejects_unsettled_debt(self):
        book=MarginPaperBook(self.shared.ledger)
        book.loans.borrow('borrow','loan','fixture','ETH','1',now_ms=100)
        with self.assertRaisesRegex(ValueError,'position or debt'):
            select_margin_pairs_offline(self.shared,self.queue,request_id='margin:blocked',
                expected_symbols=['BTCUSDT','ETHUSDT'],expected_version=0,symbols=['BTCUSDT','SOLUSDT'],
                metadata={'BTCUSDT':spot_info('BTC'),'SOLUSDT':spot_info('SOL')},
                prices={'BTCUSDT':'100','SOLUSDT':'100'},quantities={'BTCUSDT':'.1','SOLUSDT':'.1'},
                observed_at=100,clock=lambda:101)
        self.assertEqual(self.shared.ledger.balance('margin_cross:principal','ETH'),Decimal('-1'))

    def test_legacy_margin_runtime_rejects_changed_pair_set_even_without_new_asset(self):
        legacy=MarginPaperBook(self.shared.ledger)
        select_margin_pairs_offline(self.shared,self.queue,request_id='margin:btc-only',
            expected_symbols=['BTCUSDT','ETHUSDT'],expected_version=0,symbols=['BTCUSDT'],
            metadata={'BTCUSDT':spot_info('BTC')},prices={'BTCUSDT':'100'},quantities={'BTCUSDT':'.1'},
            observed_at=100,clock=lambda:101)
        with self.assertRaisesRegex(ValueError,'version superseded'):legacy._check()
        with self.assertRaisesRegex(ValueError,'runtime not integrated'):MarginPaperBook(self.shared.ledger)
        self.assertEqual(restore_margin_book(self.shared).tradable,frozenset(('BTCUSDT',)))

    def test_margin_pair_worker_preserves_dust_and_replays_after_commit(self):
        MarginPaperBook(self.shared.ledger)
        with self.shared.transaction():
            self.shared.ledger._post('fixture:eth','FIXTURE',{},[
                ('margin_cross:free','ETH',Decimal('.001')),('external:fixture','ETH',Decimal('-.001'))])
        q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        q.receive(1,True,'/mode margin_cross',now=100)
        q.receive(2,True,'/pair BTCUSDT SOLUSDT',now=100,
                  pairs={'margin_cross':{'symbols':['BTCUSDT','ETHUSDT'],'version':0}})
        token=q.db.execute('SELECT id FROM unified_pair_proposals').fetchone()[0]
        q.receive(3,True,'/confirm '+token,now=101)
        class Reader:
            def get(inner,path,params):
                self.assertFalse(self.shared.db.in_transaction);self.assertFalse(q.db.in_transaction)
                return {'symbols':[spot_info(params['symbol'][:-4])]}
            def snapshot(inner,symbol):return {'price':'100','time_ms':102000}
        def crash(stage):
            if stage=='after_financial_commit':raise RuntimeError('simulated interruption')
        try:
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                process_margin_pair(q,self.shared,lambda symbols:Reader(),clock=lambda:102,fault=crash)
            result=process_margin_pair(q,self.shared,lambda symbols:Reader(),clock=lambda:103)
            self.assertTrue(result['replayed']);self.assertEqual(q.pending('margin_cross'),[])
            self.assertEqual(self.shared.ledger.balance('margin_cross:free','ETH'),Decimal('.001'))
            self.assertEqual(self.shared.ledger.balance('margin_cross:free','SOL'),0)
            self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        finally:q.close()

    def test_margin_runner_rebinds_even_if_acknowledgement_crashes(self):
        MarginPaperBook(self.shared.ledger);q=UnifiedQueue(self.queue,allow_pair_experiment=True)
        q.receive(1,True,'/mode margin_cross',now=100)
        q.receive(2,True,'/pair BTCUSDT SOLUSDT',now=100,
                  pairs={'margin_cross':{'symbols':['BTCUSDT','ETHUSDT'],'version':0}})
        token=q.db.execute('SELECT id FROM unified_pair_proposals').fetchone()[0]
        q.receive(3,True,'/confirm '+token,now=101)
        class Reader:
            def get(inner,path,params):return {'symbols':[spot_info(params['symbol'][:-4])]}
            def snapshot(inner,symbol):return {'price':'100','time_ms':102000}
        runner=ExperimentalMarginRunner(self.shared,q,lambda symbols:Reader());old=runner.book
        def crash(stage):
            if stage=='after_financial_commit':raise RuntimeError('simulated interruption')
        try:
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                runner.pair_step(clock=lambda:102,fault=crash)
            with self.assertRaisesRegex(ValueError,'version superseded'):old._check()
            self.assertEqual(runner.book.tradable,frozenset(('BTCUSDT','SOLUSDT')))
            self.assertTrue(runner.pair_step(clock=lambda:103)['replayed'])
            self.assertEqual(q.pending('margin_cross'),[])
            self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        finally:q.close()

    def test_restored_margin_sol_position_protected_while_paused(self):
        MarginPaperBook(self.shared.ledger)
        select_margin_pairs_offline(self.shared,self.queue,request_id='margin:choose',
            expected_symbols=['BTCUSDT','ETHUSDT'],expected_version=0,symbols=['BTCUSDT','SOLUSDT'],
            metadata={'BTCUSDT':spot_info('BTC'),'SOLUSDT':spot_info('SOL')},
            prices={'BTCUSDT':'200','SOLUSDT':'200'},quantities={'BTCUSDT':'.05','SOLUSDT':'.05'},
            observed_at=100,clock=lambda:101)
        book=restore_margin_book(self.shared)
        with self.shared.transaction():
            self.shared.ledger._post('fixture:margin-usdt','FIXTURE',{},[
                ('margin_cross:free','USDT',Decimal(50)),('external:fixture','USDT',Decimal(-50))])
            self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        def quotes(stamp,sol):
            f={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],
               'min_notional':'0','max_notional':'0','observed_ms':stamp}
            return {s:{'price':sol if s=='SOLUSDT' else '200','time_ms':stamp,'server_ms':stamp,
                       'source':'fixture','price_kind':'last_public_trade','filters':f} for s in book.pairs}
        initial=quotes(1000,'200');book.record_quotes(initial,now_ms=1000)
        book.open('sol-short','sol-position','SOLUSDT','SHORT','.04','5',2,initial,now_ms=1000)
        with self.shared.transaction():self.shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
        self.shared.close();self.shared=SharedPaperStore(self.financial);self.addCleanup(self.shared.close)
        book=restore_margin_book(self.shared);now=31*900000+100
        candles={s:[(i*900000,Decimal('200')) for i in range(31)] for s in book.tradable}
        actions=MarginPaperStrategy(book).tick(quotes(now,'210'),candles,now_ms=now)
        self.assertTrue(any(a['action']=='STOP' and a['symbol']=='SOLUSDT' for a in actions))
        self.assertEqual(book._positions(),[])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

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

    def test_tampered_identity_does_not_silently_relabel_selection_history(self):
        self.choose()
        with self.shared.transaction():self.shared._set('spot','identity',{'mode':'paper','symbol':'BTCUSDT'})
        with self.assertRaisesRegex(ValueError,'history conflicts'):
            self.choose(request_id='next',expected_version=1,expected_symbol='BTCUSDT',symbol='ETHUSDT',metadata=spot_info('ETH'))

    def test_missing_or_same_database_is_rejected_without_creating_files(self):
        missing=self.queue.parent/'missing.db'
        with self.assertRaisesRegex(ValueError,'Existing queue'):
            select_spot_pair_offline(self.shared,missing,request_id='bad',expected_symbol='BTCUSDT',
                expected_version=0,symbol='SOLUSDT',metadata=spot_info(),price='100',quantity='.1',observed_at=100,clock=lambda:101)
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(ValueError,'must be distinct'):
            select_spot_pair_offline(self.shared,self.financial,request_id='bad',expected_symbol='BTCUSDT',
                expected_version=0,symbol='SOLUSDT',metadata=spot_info(),price='100',quantity='.1',observed_at=100,clock=lambda:101)

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
            client=store.prepare('BUY',Decimal('.1'),Decimal('100'),'sol-crash','fixture',quote_at=time.time())
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

    def test_experimental_shared_sol_strategy_and_paused_protection(self):
        from tests.test_pair_market_snapshot import FakeReader
        self.choose()
        class Reader(FakeReader):
            def __init__(self):super().__init__();self.price='100'
            def get(self,path,params=None):
                if path=='/api/v3/exchangeInfo':return {'symbols':[spot_info()]}
                if path=='/api/v3/ticker/price':return {'symbol':'SOLUSDT','price':self.price}
                return super().get(path,params)
        reader=Reader();store=RuntimeStore(self.financial,{'mode':'paper','symbol':'SOLUSDT'},shared=True)
        try:
            app=Application(store,PaperExchange(),reader,allow_pair_experiment=True)
            app.enable_paper_entries()
            with patch('trading_bot.app.signal_details',return_value={'action':'BUY'}):app.tick()
            self.assertGreater(store.shared.ledger.balance('spot:position','SOL'),0)
            with store.transaction():store.set('paused',True)
            reader.price='95'
            self.assertTrue(app.protective_close())
            self.assertEqual(store.shared.ledger.balance('spot:position','SOL'),0)
            self.assertTrue(store.get('paused'))
            self.assertTrue(store.shared.ledger.reconcile()['balanced'])
        finally:store.close()

    def test_isolated_selection_sol_shared_funding_and_replay(self):
        metadata=spot_info();metadata.update(contractType='PERPETUAL',marginAsset='USDT')
        args=dict(request_id='isolated:1',expected_symbol='BTCUSDT',expected_version=0,
                  symbol='SOLUSDT',metadata=metadata,price='100',quantity='.1',observed_at=100,clock=lambda:101)
        before=self.shared.snapshot('futures')
        result=select_isolated_pair_offline(self.shared,self.queue,**args)
        self.assertFalse(result['replayed'])
        self.assertTrue(select_isolated_pair_offline(self.shared,self.queue,**args)['replayed'])
        after=self.shared.snapshot('futures')
        self.assertEqual((before['cash'],before['pnl'],before['position']),(after['cash'],after['pnl'],after['position']))
        store=FuturesStore(self.financial,symbol='SOLUSDT',allow_pair_experiment=True,shared=True)
        try:
            store.command(1,True,'/resume')
            self.assertTrue(store.open_position('LONG',Decimal('100'),1000,'sol-open',metadata=metadata,quote_symbol='SOLUSDT',observed_at=time.time()))
            pos=store.get('position');self.assertEqual(pos['symbol'],'SOLUSDT')
            funding={'symbol':'SOLUSDT','time':2000,'rate':'.001','mark':'100'}
            store.apply_funding(pos['id'],[funding],2000);store.apply_funding(pos['id'],[funding],2000)
            self.assertTrue(store.close_position(Decimal('105'),2000,'sol-close','fixture',pos['id'],quote_symbol='SOLUSDT',observed_at=time.time()))
            self.assertFalse(store.close_position(Decimal('105'),2000,'sol-close','fixture',pos['id'],quote_symbol='SOLUSDT',observed_at=time.time()))
            self.assertEqual(store.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],1)
            self.assertTrue(store.shared.ledger.reconcile()['balanced'])
        finally:store.close()

    def test_isolated_selection_preserves_existing_btc_position(self):
        store=FuturesStore(self.financial,shared=True)
        try:
            store.command(1,True,'/resume');store.open_position('SHORT',Decimal('100'),1000,'btc-open')
            store.command(2,True,'/pause');before=store.get('position')
            metadata=spot_info();metadata.update(contractType='PERPETUAL',marginAsset='USDT')
            with self.assertRaisesRegex(ValueError,'Existing futures position'):
                select_isolated_pair_offline(self.shared,self.queue,request_id='isolated:blocked',expected_symbol='BTCUSDT',
                    expected_version=0,symbol='SOLUSDT',metadata=metadata,price='100',quantity='.1',observed_at=100,clock=lambda:101)
            self.assertEqual(store.get('position'),before)
        finally:store.close()

    def test_relabelled_open_futures_position_blocks_reconciliation(self):
        store=FuturesStore(self.financial,shared=True)
        try:
            store.command(1,True,'/resume');store.open_position('LONG',Decimal('100'),1000,'btc-open')
            with self.shared.transaction():
                identity=self.shared._get('futures','identity');identity['symbol']='SOLUSDT'
                self.shared._set('futures','identity',identity)
            with self.assertRaisesRegex(ValueError,'futures position symbol'):
                self.shared._assert_consistent('futures')
        finally:store.close()

    def test_isolated_sol_funding_recovers_across_commit_crashes(self):
        metadata=spot_info();metadata.update(contractType='PERPETUAL',marginAsset='USDT')
        select_isolated_pair_offline(self.shared,self.queue,request_id='isolated:crash',expected_symbol='BTCUSDT',
            expected_version=0,symbol='SOLUSDT',metadata=metadata,price='100',quantity='.1',observed_at=100,clock=lambda:101)
        store=FuturesStore(self.financial,symbol='SOLUSDT',allow_pair_experiment=True,shared=True)
        try:
            store.command(1,True,'/resume')
            store.open_position('SHORT',Decimal('100'),1000,'open-crash',metadata=metadata,quote_symbol='SOLUSDT',observed_at=time.time())
            ident=store.get('position')['id']
        finally:store.close()
        script='''import os,sys\nfrom trading_bot.futures_store import FuturesStore\ns=FuturesStore(sys.argv[1],symbol="SOLUSDT",allow_pair_experiment=True,shared=True)\ndef fault(stage):\n if stage==sys.argv[3]:os._exit(74)\ns.fault=fault\ns.apply_funding(sys.argv[2],[{"symbol":"SOLUSDT","time":2000,"rate":".001","mark":"100"}],2000)\n'''
        for stage in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.financial),ident,stage],capture_output=True,timeout=20)
            self.assertEqual(child.returncode,74,child.stderr.decode())
            check=FuturesStore(self.financial,symbol='SOLUSDT',allow_pair_experiment=True,shared=True)
            try:
                self.assertEqual(check.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],int(stage=='after_commit'))
                self.assertTrue(check.shared.ledger.reconcile()['balanced'])
            finally:check.close()
        check=FuturesStore(self.financial,symbol='SOLUSDT',allow_pair_experiment=True,shared=True)
        try:
            event={'symbol':'SOLUSDT','time':2000,'rate':'.001','mark':'100'}
            check.apply_funding(ident,[event],2000)
            self.assertEqual(check.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],1)
        finally:check.close()
