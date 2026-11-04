import tempfile
import unittest
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from tests.test_paper_pair_catalog import spot_info
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_data_sync import CrossDataSync
from trading_bot.cross_paper_strategy import CrossPaperStrategy
from trading_bot.testnet_observer import ObservationError
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_pair_selection_offline import select_cross_pairs_offline
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class PairCrossOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        s=RuntimeStore(root/'spot.db',{'mode':'paper','symbol':'BTCUSDT'});f=FuturesStore(root/'future.db')
        s.close();f.close();consolidate_snapshots(root/'spot.db',root/'future.db',root/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(root/'shared.db');self.addCleanup(self.shared.close)
        self.book=CrossPaperBook(self.shared,allow_pair_experiment=True)
        proposal=self.shared.propose_transfer('fixture:cross','futures_isolated','futures_cross','50',now=99)
        self.shared.confirm_transfer(proposal['id'],now=99)
        self.queue=root/'queue.db';q=UnifiedQueue(self.queue);q.close()

    @staticmethod
    def info(base):
        result=spot_info(base);result.update(contractType='PERPETUAL',marginAsset='USDT')
        result['filters'][-1]['minNotional']='4'
        return result

    @staticmethod
    def quote(mark,stamp):
        return {'mark':mark,'time':stamp,'funding_complete':True,'server_ms':int(stamp*1000),'next_due_ms':28000000,'interval_ms':28800000}

    def choose(self,**changes):
        args=dict(request_id='cross:choose',expected_symbols=['BTCUSDT','ETHUSDT'],expected_version=0,
                  symbols=['BTCUSDT','SOLUSDT'],metadata={'BTCUSDT':self.info('BTC'),'SOLUSDT':self.info('SOL')},
                  prices={'BTCUSDT':'100','SOLUSDT':'100'},quantities={'BTCUSDT':'.1','SOLUSDT':'.1'},
                  observed_at=100,clock=lambda:101)
        args.update(changes)
        return select_cross_pairs_offline(self.shared,self.queue,**args)

    def test_cross_sol_open_close_replay_and_asset_risk(self):
        before=self.shared.ledger.balance('futures_cross:free')
        result=self.choose();self.assertEqual(result['symbols'],['BTCUSDT','SOLUSDT'])
        self.assertTrue(self.choose()['replayed']);self.assertEqual(self.shared.ledger.balance('futures_cross:free'),before)
        quotes={'BTCUSDT':self.quote('100',102),'SOLUSDT':self.quote('100',102)}
        self.book.record_quotes(quotes,now=102);self.book.set_paused(False,now=102)
        pos=self.book.open('sol-open','SOLUSDT','LONG','5',1,quotes,now=102,metadata=self.info('SOL'),metadata_at=101)
        self.assertEqual(pos['symbol'],'SOLUSDT');self.assertEqual(Decimal(pos['quantity']),Decimal('.049'))
        risk=self.book.risk(quotes,now=102);report=live_report(self.shared,now=102)
        self.assertEqual(report['total'],Decimal('1950')+risk['equity']-risk['exit_cost'])
        self.assertEqual(report['cross']['positions'][0]['symbol'],'SOLUSDT')
        self.assertEqual(self.book.open('sol-open','SOLUSDT','LONG','5',1,quotes,now=102,metadata=self.info('SOL'),metadata_at=101),pos)
        result=self.book.close('sol-close','SOLUSDT',pos['id'],pos['quantity'],quotes,now=103)
        self.assertEqual(self.book.close('sol-close','SOLUSDT',pos['id'],pos['quantity'],quotes,now=103),result)
        self.assertEqual(len(self.book._positions()),0);self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_existing_position_or_unexpected_quote_blocks_change(self):
        legacy={'BTCUSDT':self.quote('100',100),'ETHUSDT':self.quote('200',100)}
        self.book.record_quotes(legacy,now=100);self.book.set_paused(False,now=100)
        position=self.book.open('old','BTCUSDT','LONG','5',1,legacy,now=100)
        self.book.set_paused(True,now=100)
        with self.assertRaisesRegex(ValueError,'Existing cross positions'):
            self.choose()
        self.assertEqual(self.book._positions()[0]['id'],position['id'])

    def test_missing_symbol_data_blocks_resume(self):
        self.choose()
        quotes={'BTCUSDT':self.quote('100',102),'ETHUSDT':self.quote('200',102)}
        with self.assertRaisesRegex(ValueError,'Incomplete cross data'):
            self.book.record_quotes(quotes,now=102)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)

    def test_rounding_below_minimum_notional_does_not_open_or_debit(self):
        self.choose();quotes={'BTCUSDT':self.quote('100',102),'SOLUSDT':self.quote('100',102)}
        self.book.record_quotes(quotes,now=102);self.book.set_paused(False,now=102)
        info=self.info('SOL');info['filters'][-1]['minNotional']='5'
        before=self.shared.ledger.balance('futures_cross:free')
        with self.assertRaisesRegex(ValueError,'notional_outside_filters'):
            self.book.open('too-small','SOLUSDT','LONG','5',1,quotes,now=102,metadata=info,metadata_at=101)
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),before)
        self.assertEqual(self.book._positions(),[])

    def test_sol_funding_is_accounted_once(self):
        self.choose();quotes={'BTCUSDT':self.quote('100',102),'SOLUSDT':self.quote('100',102)}
        self.book.record_quotes(quotes,now=102);self.book.set_paused(False,now=102)
        pos=self.book.open('sol-funding-open','SOLUSDT','SHORT','5',1,quotes,now=102,metadata=self.info('SOL'),metadata_at=101)
        result=self.book.funding('sol-funding','SOLUSDT',pos['id'],28000000,0,'.001','100','fixture',now=28000)
        self.assertEqual(result,self.book.funding('sol-funding','SOLUSDT',pos['id'],28000000,0,'.001','100','fixture',now=28001))
        expected=Decimal(pos['quantity'])*Decimal('100')*Decimal('.001')
        self.assertEqual(self.shared.ledger.balance('futures_cross:funding'),expected)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_funding').fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_cannot_remove_sol_while_its_position_is_open(self):
        self.choose();quotes={'BTCUSDT':self.quote('100',102),'SOLUSDT':self.quote('100',102)}
        self.book.record_quotes(quotes,now=102);self.book.set_paused(False,now=102)
        pos=self.book.open('sol-risk','SOLUSDT','LONG','5',1,quotes,now=102,metadata=self.info('SOL'),metadata_at=101)
        self.book.set_paused(True,now=102)
        with self.assertRaisesRegex(ValueError,'Existing cross positions'):
            self.choose(request_id='remove',expected_symbols=['BTCUSDT','SOLUSDT'],expected_version=1,
                symbols=['BTCUSDT','ETHUSDT'],metadata={'BTCUSDT':self.info('BTC'),'ETHUSDT':self.info('ETH')},
                prices={'BTCUSDT':'100','ETHUSDT':'100'},quantities={'BTCUSDT':'.1','ETHUSDT':'.1'})
        self.assertEqual(self.book._positions()[0]['id'],pos['id'])

    def test_selection_commit_crash_preserves_funds_and_replays(self):
        script='''import os,sys\nfrom tests.test_pair_cross_offline import PairCrossOfflineTests\nfrom trading_bot.paper_shared_store import SharedPaperStore\nfrom trading_bot.paper_pair_selection_offline import select_cross_pairs_offline\ns=SharedPaperStore(sys.argv[1])\ndef fault(stage):\n if stage==sys.argv[3]:os._exit(74)\ns.fault=fault\nf=PairCrossOfflineTests.info\nselect_cross_pairs_offline(s,sys.argv[2],request_id="crash",expected_symbols=["BTCUSDT","ETHUSDT"],expected_version=0,symbols=["BTCUSDT","SOLUSDT"],metadata={"BTCUSDT":f("BTC"),"SOLUSDT":f("SOL")},prices={"BTCUSDT":"100","SOLUSDT":"100"},quantities={"BTCUSDT":".1","SOLUSDT":".1"},observed_at=100,clock=lambda:101)\n'''
        before=self.shared.ledger.balance('futures_cross:free')
        for stage in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.shared.db.execute('PRAGMA database_list').fetchone()[2]),str(self.queue),stage],capture_output=True,timeout=20)
            self.assertEqual(child.returncode,74,child.stderr.decode())
            self.assertEqual(self.book._symbols(),('BTCUSDT','SOLUSDT') if stage=='after_commit' else ('BTCUSDT','ETHUSDT'))
            self.assertEqual(self.shared.ledger.balance('futures_cross:free'),before)
        self.assertTrue(self.choose(request_id='crash')['replayed'])

    def test_removed_symbol_funding_history_still_loaded(self):
        legacy={'BTCUSDT':self.quote('100',100),'ETHUSDT':self.quote('200',100)}
        self.book.record_quotes(legacy,now=100);self.book.set_paused(False,now=100)
        old=self.book.open('old-eth','ETHUSDT','LONG','5',1,legacy,now=100)
        self.book.close('old-eth-close','ETHUSDT',old['id'],old['quantity'],legacy,now=101)
        self.book.set_paused(True,now=101);self.choose()
        class Reader:
            def __init__(self):self.loaded=[]
            def schedule(self,symbol):
                return {'symbol':symbol,'mark':'100','server_ms':102000,'next_due_ms':28000000,'interval_ms':28800000}
            def funding(self,symbol,start,end):self.loaded.append(symbol);return []
        reader=Reader();quotes=CrossDataSync(self.book,reader).refresh(now=102)
        self.assertIn('ETHUSDT',reader.loaded)
        self.assertEqual(set(quotes),{'BTCUSDT','SOLUSDT'})
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        class BrokenReader(Reader):
            def funding(self,symbol,start,end):
                if symbol=='ETHUSDT':raise RuntimeError('fixture internal error')
                return []
        with self.assertRaisesRegex(RuntimeError,'internal error'):
            CrossDataSync(self.book,BrokenReader()).refresh(now=102)

    def test_unavailable_removed_symbol_keeps_current_protection_and_pauses_entries(self):
        legacy={'BTCUSDT':self.quote('100',100),'ETHUSDT':self.quote('200',100)}
        self.book.record_quotes(legacy,now=100);self.book.set_paused(False,now=100)
        old=self.book.open('old-eth','ETHUSDT','LONG','5',1,legacy,now=100)
        self.book.close('old-eth-close','ETHUSDT',old['id'],old['quantity'],legacy,now=101)
        self.book.set_paused(True,now=101);self.choose()
        active={'BTCUSDT':self.quote('100',102),'SOLUSDT':self.quote('100',102)}
        self.book.record_quotes(active,now=102);self.book.set_paused(False,now=102)
        sol=self.book.open('sol-risk','SOLUSDT','LONG','5',1,active,now=102,metadata=self.info('SOL'),metadata_at=101)
        class Reader:
            available=False
            def schedule(self,symbol):
                return {'symbol':symbol,'mark':'90' if symbol=='SOLUSDT' else '100','server_ms':27999000,'next_due_ms':28000000,'interval_ms':28800000}
            def funding(self,symbol,start,end):
                if symbol=='ETHUSDT' and not self.available:raise ObservationError('offline historical source unavailable')
                return []
        reader=Reader();sync=CrossDataSync(self.book,reader);quotes=sync.refresh(now=27999)
        self.assertEqual(sync.model_status,'historical_unconfirmed')
        self.assertEqual(self.book.runtime_error(),'HistoricalFundingUnavailable')
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        report=live_report(self.shared,now=27999)
        self.assertIsNone(report['total']);self.assertIsNotNone(report['cross']['gross'])
        candles={s:[(n*900000,Decimal('100')) for n in range(31)] for s in ('BTCUSDT','SOLUSDT')}
        actions=CrossPaperStrategy(self.book).tick(quotes,candles,27999000,now=27999)
        self.assertTrue(any(action['action']=='STOP' and action['symbol']=='SOLUSDT' for action in actions))
        self.assertEqual(self.book._positions(),[])
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        reader.available=True;sync.refresh(now=27999)
        self.assertIsNone(self.book.runtime_error())
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)

    def _exercise_retired_funding_conflict(self, mode):
        legacy={'BTCUSDT':self.quote('100',100),'ETHUSDT':self.quote('200',100)}
        self.book.record_quotes(legacy,now=100);self.book.set_paused(False,now=100)
        old=self.book.open('old-eth','ETHUSDT','LONG','5',1,legacy,now=100)
        class Reader:
            mode='normal';server_ms=28001000
            def schedule(self,symbol):
                return {'symbol':symbol,'mark':'90' if symbol=='SOLUSDT' else '100',
                        'server_ms':self.server_ms,'next_due_ms':56800000,'interval_ms':28800000}
            def funding(self,symbol,start,end):
                if symbol!='ETHUSDT' or self.mode=='missing':return []
                event={'symbol':'ETHUSDT','time':28000001,'rate_type':'Regular',
                       'rate':'.002' if self.mode=='changed' else '.001','mark':'100'}
                return [event,dict(event,time=28000002)] if self.mode=='additional' else [event]
        reader=Reader();quotes=CrossDataSync(self.book,reader).refresh(now=28001)
        self.book.close('old-eth-close','ETHUSDT',old['id'],old['quantity'],quotes,now=28001)
        self.book.set_paused(True,now=28001)
        self.choose(observed_at=28002,clock=lambda:28002)
        active={'BTCUSDT':{**self.quote('100',28002),'next_due_ms':56800000},
                'SOLUSDT':{**self.quote('100',28002),'next_due_ms':56800000}}
        self.book.record_quotes(active,now=28002);self.book.set_paused(False,now=28002)
        self.book.open('sol-risk','SOLUSDT','LONG','5',1,active,now=28002,metadata=self.info('SOL'),metadata_at=28002)
        before=[tuple(x) for x in self.shared.db.execute('SELECT * FROM cross_funding')]
        reader.mode=mode;reader.server_ms=28003000
        sync=CrossDataSync(self.book,reader);quotes=sync.refresh(now=28003)
        self.assertEqual(sync.model_status,'historical_contradiction')
        self.assertEqual(before,[tuple(x) for x in self.shared.db.execute('SELECT * FROM cross_funding')])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        self.assertIsNone(live_report(self.shared,now=28003)['total'])
        self.book.risk(quotes,now=28003)
        path=self.shared.db.execute('PRAGMA database_list').fetchone()[2]
        self.shared.close();self.shared=SharedPaperStore(path);self.addCleanup(self.shared.close)
        self.book=CrossPaperBook(self.shared,allow_pair_experiment=True)
        sync=CrossDataSync(self.book,reader);quotes=sync.refresh(now=28003)
        self.assertEqual(sync.model_status,'historical_contradiction')
        self.assertEqual(before,[tuple(x) for x in self.shared.db.execute('SELECT * FROM cross_funding')])
        self.assertEqual(self.book._positions()[0]['symbol'],'SOLUSDT')
        with self.assertRaisesRegex(ValueError,'requires recovery'):self.book.set_paused(False,now=28003)
        candles={s:[(n*900000,Decimal('100')) for n in range(31)] for s in ('BTCUSDT','SOLUSDT')}
        actions=CrossPaperStrategy(self.book).tick(quotes,candles,28003000,now=28003)
        self.assertTrue(any(a['action']=='STOP' and a['symbol']=='SOLUSDT' for a in actions))
        self.assertEqual(self.book._positions(),[])

    def test_changed_retired_funding_preserves_current_protection(self):
        self._exercise_retired_funding_conflict('changed')

    def test_missing_retired_funding_preserves_current_protection(self):
        self._exercise_retired_funding_conflict('missing')

    def test_additional_retired_funding_preserves_current_protection(self):
        self._exercise_retired_funding_conflict('additional')

    def test_retired_funding_missing_changed_and_extra_are_detected(self):
        legacy={'BTCUSDT':self.quote('100',100),'ETHUSDT':self.quote('200',100)}
        self.book.record_quotes(legacy,now=100);self.book.set_paused(False,now=100)
        self.book.open('old-eth','ETHUSDT','LONG','5',1,legacy,now=100)
        event={'symbol':'ETHUSDT','time':28000001,'rate_type':'Regular','rate':'.001','mark':'100'}
        class Reader:
            def schedule(self,symbol):
                return {'symbol':symbol,'mark':'100','server_ms':28001000,'next_due_ms':56800000,'interval_ms':28800000}
            def funding(self,symbol,start,end):return [event] if symbol=='ETHUSDT' else []
        sync=CrossDataSync(self.book,Reader());sync.refresh(now=28001)
        before=[tuple(x) for x in self.shared.db.execute('SELECT * FROM cross_funding')]
        for name,events in [('missing',[]),('changed',[dict(event,rate='.002')]),
                            ('additional',[event,dict(event,time=28000002)])]:
            with self.subTest(name=name),self.assertRaises(ValueError):
                sync._check_retired_history('ETHUSDT',events)
            self.assertEqual(before,[tuple(x) for x in self.shared.db.execute('SELECT * FROM cross_funding')])
