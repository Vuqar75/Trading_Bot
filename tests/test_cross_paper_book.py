import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore


class CrossPaperBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); spot = root/'spot.db'; future = root/'future.db'; self.path = root/'shared.db'
        a=RuntimeStore(spot,{'mode':'paper','interval':'15m'}); b=FuturesStore(future)
        a.close();b.close()
        consolidate_snapshots(spot,future,self.path,captured_at=1)
        self.shared=SharedPaperStore(self.path);self.addCleanup(self.shared.close)
        self.book=CrossPaperBook(self.shared)
        proposal=self.shared.propose_transfer('initial','futures_isolated','futures_cross','50',now=99)
        self.shared.confirm_transfer(proposal['id'],now=99)
        self.quotes={'BTCUSDT':{'mark':'100','time':100,'funding_complete':True},'ETHUSDT':{'mark':'200','time':100,'funding_complete':True}}
        self.book.record_quotes(self.quotes,now=100)
        self.book.set_paused(False,now=100)

    def positions(self):
        btc=self.book.open('btc','BTCUSDT','LONG','5',1,self.quotes,now=100)
        eth=self.book.open('eth','ETHUSDT','SHORT','5',1,self.quotes,now=100)
        return btc,eth

    def test_opposite_results_affect_same_equity_and_available(self):
        btc,eth=self.positions()
        changed={'BTCUSDT':{'mark':'80','time':101,'funding_complete':True},'ETHUSDT':{'mark':'180','time':101,'funding_complete':True}}
        base=self.book.risk(self.quotes,now=100);self.book.record_quotes(changed,now=101);risk=self.book.risk(changed,now=101)
        self.assertLess(risk['equity'],base['equity']);self.assertLess(risk['available'],base['available'])
        self.assertEqual(risk['margin'],Decimal('10'));self.assertEqual(len(risk['positions']),2)
        self.assertEqual(self.book.open('btc','BTCUSDT','LONG','5',1,self.quotes,now=100),btc)
        self.assertEqual(self.shared.ledger.balance('spot:free'),Decimal('1000'))

    def test_stale_or_incomplete_pair_and_pause_block_entry(self):
        self.book.set_paused(True)
        with self.assertRaisesRegex(ValueError,'paused'):self.book.open('a','BTCUSDT','LONG','5',1,self.quotes,now=100)
        self.book.set_paused(False,now=100)
        with self.assertRaisesRegex(ValueError,'Stale'):self.book.open('a','BTCUSDT','LONG','5',1,self.quotes,now=221)
        bad={**self.quotes,'ETHUSDT':{'mark':'200','time':100,'funding_complete':False}}
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.book.open('a','BTCUSDT','LONG','5',1,bad,now=100)
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal('50'))

    def test_funding_deduplication_and_explicit_revision(self):
        btc,eth=self.positions()
        x=self.book.funding('fund-1','BTCUSDT',btc['id'],10,0,'.001','100','fixture',now=100)
        self.assertEqual(self.book.funding('fund-1','BTCUSDT',btc['id'],10,0,'.001','100','fixture',now=101),x)
        self.assertLess(Decimal(x['value']),0)
        y=self.book.funding('fund-2','ETHUSDT',eth['id'],10,0,'.001','200','fixture',now=100)
        self.assertGreater(Decimal(y['value']),0)
        correction=self.book.funding('fund-1-correction','BTCUSDT',btc['id'],10,1,'.002','100','corrected fixture',now=100)
        self.assertEqual(Decimal(correction['delta']),Decimal(x['value']))
        with self.assertRaisesRegex(ValueError,'revision'):self.book.funding('another','BTCUSDT',btc['id'],10,1,'.002','100','fixture',now=100)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_shared_liquidation_records_deficit_and_preserves_other_wallets(self):
        self.positions()
        # Synthetic catastrophic price move exercises the explicit model write-off.
        mark={'BTCUSDT':{'mark':'1','time':101,'funding_complete':True},'ETHUSDT':{'mark':'4000','time':101,'funding_complete':True}}
        self.book.record_quotes(mark,now=101)
        risk=self.book.risk(mark,now=101);self.assertLessEqual(risk['buffer'],0)
        p=risk['positions'][0]
        result=self.book.close('liquidate',p['symbol'],p['id'],p['quantity'],mark,now=101,liquidate=True)
        self.assertEqual(len(result['closed']),2);self.assertGreater(Decimal(result['adjustment']),0)
        self.assertEqual(self.book.risk(mark,now=101)['positions'],[])
        self.assertEqual(self.shared.ledger.balance('spot:free'),Decimal('1000'))
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),Decimal('950'))
        self.assertEqual(self.shared.db.execute("select count(*) from ledger_events where kind='CROSS_LIQUIDATION_ADJUSTMENT'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_transfer_out_rechecks_collateral_and_fresh_both_pairs(self):
        self.positions();self.book.record_quotes(self.quotes,now=100);self.book.set_paused(True)
        with self.assertRaisesRegex(ValueError,'collateral'):
            self.shared.propose_transfer('too-much','futures_cross','reserve','41',now=100)
        with self.assertRaisesRegex(ValueError,'Stale'):
            self.shared.propose_transfer('stale','futures_cross','reserve','1',now=221)
        proposal=self.shared.propose_transfer('safe','futures_cross','reserve','1',now=100)
        self.book.set_paused(False,now=100)
        with self.assertRaisesRegex(ValueError,'paused'):self.shared.confirm_transfer(proposal['id'],now=101)
        self.book.set_paused(True);self.shared.confirm_transfer(proposal['id'],now=101)
        self.assertEqual(self.shared.confirm_transfer(proposal['id'],now=102),'transfer:'+proposal['id'])
        self.assertEqual(self.shared.ledger.balance('reserve:free'),Decimal(1))
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_positions').fetchone()[0],2)

    def test_concurrent_open_serializes_same_symbol(self):
        barrier=threading.Barrier(2);out=[]
        def run(i):
            other=SharedPaperStore(self.path)
            try:
                book=CrossPaperBook(other);barrier.wait()
                try:book.open('concurrent-'+str(i),'BTCUSDT','LONG','5',1,self.quotes,now=100);out.append('ok')
                except ValueError:out.append('blocked')
            finally:other.close()
        threads=[threading.Thread(target=run,args=(i,)) for i in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sorted(out),['blocked','ok']);self.assertEqual(len(self.book.risk(self.quotes,now=100)['positions']),1)

    def test_funding_invalidation_blocks_exit_and_entry(self):
        btc,_=self.positions();self.book.invalidate_data('ETHUSDT')
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            self.book.close('close','BTCUSDT',btc['id'],btc['quantity'],self.quotes,now=100)
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.book.risk(self.quotes,now=100)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_positions').fetchone()[0],2)

    def test_settings_require_pause_and_no_open_positions(self):
        with self.assertRaisesRegex(ValueError,'Pause'):self.book.set_settings(leverage=2,interval='15m')
        self.book.set_paused(True);self.book.set_settings(leverage=2,interval='15m')
        self.book.set_paused(False,now=100)
        with self.assertRaisesRegex(ValueError,'Leverage'):self.book.open('wrong','BTCUSDT','LONG','5',1,self.quotes,now=100)
        self.book.open('right','BTCUSDT','LONG','5',2,self.quotes,now=100)
        self.book.set_paused(True)
        with self.assertRaisesRegex(ValueError,'close'):self.book.set_settings(leverage=1,interval='15m')

    def test_position_tampering_is_detected(self):
        self.positions()
        with sqlite3.connect(self.path) as db:db.execute("UPDATE cross_positions SET entry='999' WHERE symbol='BTCUSDT'")
        with self.assertRaisesRegex(ValueError,'journal mismatch'):self.book.risk(self.quotes,now=100)

    def test_open_and_withdraw_cannot_both_spend_same_collateral(self):
        barrier=threading.Barrier(2);out=[]
        def entry():
            shared=SharedPaperStore(self.path)
            try:
                book=CrossPaperBook(shared);barrier.wait()
                try:book.open('race-entry','BTCUSDT','LONG','5',1,self.quotes,now=100);out.append('entered')
                except ValueError:out.append('entry-blocked')
            finally:shared.close()
        def transfer():
            shared=SharedPaperStore(self.path)
            try:
                book=CrossPaperBook(shared);barrier.wait();book.set_paused(True)
                try:
                    p=shared.propose_transfer('race-transfer','futures_cross','reserve','46',now=100)
                    shared.confirm_transfer(p['id'],now=100);out.append('transferred')
                except ValueError:out.append('transfer-blocked')
            finally:shared.close()
        threads=[threading.Thread(target=entry),threading.Thread(target=transfer)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(out),2);self.assertFalse('entered' in out and 'transferred' in out)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_process_death_before_and_after_open_commit_is_idempotent(self):
        code=("import os,sys,json; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from trading_bot.cross_paper_book import CrossPaperBook; "
              "s=SharedPaperStore(sys.argv[1]); b=CrossPaperBook(s); "
              "s.fault=lambda p: os._exit(73) if p==sys.argv[2] else None; "
              "b.open('crash','BTCUSDT','LONG','5',1,json.loads(sys.argv[3]),now=100)")
        for point in ('before_commit','after_commit'):
            p=subprocess.run([sys.executable,'-c',code,str(self.path),point,json.dumps(self.quotes)])
            self.assertEqual(p.returncode,73)
            risk=self.book.risk(self.quotes,now=100)
            self.assertEqual(len(risk['positions']),int(point=='after_commit'))
            self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        self.assertEqual(self.book.open('crash','BTCUSDT','LONG','5',1,self.quotes,now=100)['symbol'],'BTCUSDT')
        self.assertEqual(len(self.book.risk(self.quotes,now=100)['positions']),1)


if __name__ == '__main__':
    unittest.main()
