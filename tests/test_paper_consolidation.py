import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.app import Application
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.outbox_relay import FuturesOutboxRelay
from trading_bot.app import TelegramTransport
from trading_bot.unified_receiver import MarketReceiver
from trading_bot.futures_app import FuturesApplication
from trading_bot.paper_rollback_guard import check_rollback


class PaperConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.spot=root/'spot-snapshot.db';self.future=root/'future-snapshot.db';self.target=root/'consolidated.db'
        spot=RuntimeStore(root/'spot.db',{'mode':'paper','interval':'15m'})
        future=FuturesStore(root/'futures.db')
        try:
            app=Application(spot,PaperExchange(),None);spot.command(1,True,'/resume');app.execute('BUY','.1',Decimal(100),'entry','TEST')
            future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),1000,'futures-entry')
            p=future.get('position');future.apply_funding(p['id'],[{'time':1500,'rate':'.001','mark':'100'}],2000)
            with spot.transaction():spot.set('paused',True)
            with future.transaction():future.set('paused',True)
            self.expected_spot=spot.get('position');self.expected_future=future.get('position')
            for src,dest in ((spot.db,self.spot),(future.db,self.future)):
                with sqlite3.connect(dest) as out:src.backup(out)
        finally:spot.close();future.close()
        self.before=[hashlib.sha256(x.read_bytes()).hexdigest() for x in (self.spot,self.future)]

    def test_consolidates_history_position_funding_and_separate_outboxes(self):
        result=consolidate_snapshots(self.spot,self.future,self.target,captured_at=1000)
        self.assertEqual(result['spot_position'],self.expected_spot)
        self.assertEqual(result['futures_position'],self.expected_future)
        self.assertEqual(self.before,[hashlib.sha256(x.read_bytes()).hexdigest() for x in (self.spot,self.future)])
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('select count(*) from runtime_fills').fetchone()[0],1)
            self.assertEqual(db.execute('select count(*) from futures_funding').fetchone()[0],1)
            self.assertGreater(db.execute('select count(*) from runtime_outbox').fetchone()[0],0)
            self.assertGreater(db.execute('select count(*) from futures_outbox').fetchone()[0],0)
            self.assertEqual(json.loads(db.execute("select value from futures_meta where key='position'").fetchone()[0]),self.expected_future)
            self.assertEqual(db.execute("select count(*) from ledger_events where kind='OPENING'").fetchone()[0],1)
        with self.assertRaises(ValueError):consolidate_snapshots(self.spot,self.future,self.target,captured_at=1000)

    def test_process_death_before_publish_never_exposes_partial_target(self):
        code=("import os,sys; from trading_bot.paper_consolidation import consolidate_snapshots; "
              "consolidate_snapshots(*sys.argv[1:],captured_at=1,fault=lambda p: os._exit(71) if p=='before_publish' else None)")
        p=subprocess.run([sys.executable,'-c',code,str(self.spot),str(self.future),str(self.target)])
        self.assertEqual(p.returncode,71);self.assertFalse(self.target.exists())
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        self.assertTrue(self.target.exists())

    def test_process_death_after_publish_leaves_valid_target_and_no_retry_overwrite(self):
        code=("import os,sys; from trading_bot.paper_consolidation import consolidate_snapshots; "
              "consolidate_snapshots(*sys.argv[1:],captured_at=1,fault=lambda p: os._exit(72) if p=='after_publish' else None)")
        p=subprocess.run([sys.executable,'-c',code,str(self.spot),str(self.future),str(self.target)])
        self.assertEqual(p.returncode,72);self.assertTrue(self.target.exists())
        with sqlite3.connect(self.target) as db:self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
        with self.assertRaises(ValueError):consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)

    def test_shared_financial_mutation_is_atomic_idempotent_and_journaled(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        shared=SharedPaperStore(self.target);self.addCleanup(shared.close)
        before=shared.snapshot('spot');old=Decimal(before['cash'])
        def buy(db):
            p=shared._get('spot','position')
            shared._set('spot','cash',str(old-Decimal('5.005')))
            shared._set('spot','position',{**p,'quantity':str(Decimal(p['quantity'])+Decimal('.05'))})
            return {'trade':'simulated'}
        self.assertEqual(shared.apply('spot','op-1',{'kind':'synthetic fill'},buy),{'trade':'simulated'})
        self.assertEqual(shared.ledger.balance('spot:free'),old-Decimal('5.005'))
        self.assertEqual(shared.ledger.balance('spot:position','BTC'),Decimal('.15'))
        self.assertEqual(shared.apply('spot','op-1',{'kind':'synthetic fill'},lambda db: self.fail('duplicate executed'))['trade'],'simulated')
        self.assertEqual(shared.db.execute("select count(*) from ledger_events where id='shared:op-1'").fetchone()[0],1)
        self.assertTrue(shared.ledger.reconcile()['balanced'])
        with self.assertRaises(ValueError):shared.apply('spot','op-1',{'kind':'different'},buy)

    def test_shared_mutation_crash_before_and_after_commit(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        with sqlite3.connect(self.target) as db:old=Decimal(json.loads(db.execute("select value from runtime_meta where key='cash'").fetchone()[0]))
        code=("import os,sys,json; from trading_bot.paper_shared_store import SharedPaperStore; "
              "s=SharedPaperStore(sys.argv[1]); point=sys.argv[2]; "
              "s.fault=lambda p: os._exit(73) if p==point else None; "
              "s.apply('spot','crash-op',{'kind':'test'},lambda db: s._set('spot','cash',str(int(sys.argv[3]))))")
        new=str(int(old)-1)
        for point in ('before_commit','after_commit'):
            result=subprocess.run([sys.executable,'-c',code,str(self.target),point,new])
            self.assertEqual(result.returncode,73)
            shared=SharedPaperStore(self.target)
            try:
                current=Decimal(shared.snapshot('spot')['cash'])
                if point=='before_commit':self.assertEqual(current,old)
                else:self.assertEqual(current,Decimal(new))
                self.assertEqual(shared.ledger.balance('spot:free'),current)
                self.assertTrue(shared.ledger.reconcile()['balanced'])
            finally:shared.close()

    def test_shared_drift_blocks_writes(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        with sqlite3.connect(self.target) as db:db.execute("update runtime_meta set value='\"1\"' where key='cash'")
        with self.assertRaisesRegex(ValueError,'mismatch'):SharedPaperStore(self.target)

    def test_opening_anchor_canonicalizes_exponent_cash_without_losing_value(self):
        with sqlite3.connect(self.spot) as db:db.execute("update runtime_meta set value=? where key='cash'",(json.dumps('1E-8'),))
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        shared=SharedPaperStore(self.target);self.addCleanup(shared.close)
        self.assertEqual(shared.ledger.balance('spot:free'),Decimal('0.00000001'))
        self.assertEqual(Decimal(shared.snapshot('spot')['cash']),Decimal('1E-8'))

    def test_rollback_gate_accepts_unmodified_candidate_and_rejects_transfer(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        self.assertTrue(check_rollback(self.target,self.spot,self.future)['safe_for_financial_rollback'])
        shared=SharedPaperStore(self.target)
        try:
            p=shared.propose_transfer('rollback-test','spot','reserve','1',now=1);shared.confirm_transfer(p['id'],now=2)
        finally:shared.close()
        result=check_rollback(self.target,self.spot,self.future)
        self.assertFalse(result['safe_for_financial_rollback']);self.assertIn('new_financial_events',result['reasons'])

    def test_rollback_gate_rejects_protective_exit_and_funding_changes(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        future=FuturesStore(self.target,shared=True)
        try:
            pos=future.get('position');future.close_position(Decimal(100),2000,'rollback-close','STOP',pos['id'])
        finally:future.close()
        result=check_rollback(self.target,self.spot,self.future)
        self.assertFalse(result['safe_for_financial_rollback']);self.assertIn('futures_changed',result['reasons'])

    def test_shared_financial_drift_blocks_resume_but_allows_pause(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        with sqlite3.connect(self.target) as db:db.execute("update runtime_meta set value='\"7\"' where key='pnl'")
        r=MarketReceiver(self.target,'spot');self.addCleanup(r.close)
        with self.assertRaisesRegex(ValueError,'mismatch'):r.apply(99,'/resume')
        self.assertIn('паузе',r.apply(100,'/pause'))

    def test_deployment_hold_blocks_entries_transfers_and_manual_close_not_protection(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True);future=FuturesStore(self.target,shared=True)
        self.addCleanup(spot.close);self.addCleanup(future.close)
        spot.shared.set_deployment_hold(True)
        receiver=MarketReceiver(self.target,'spot');self.addCleanup(receiver.close)
        self.assertIn('заблокированы',receiver.apply(100,'/resume'))
        self.assertTrue(spot.get('paused'))
        with self.assertRaises(ValueError):spot.shared.propose_transfer('held','spot','reserve','1')
        with self.assertRaises(Exception):Application(spot,PaperExchange(),None).enable_paper_entries()
        p=future.get('position')
        self.assertTrue(future.close_position(Decimal(100),2000,'hold-protection','STOP',p['id']))
        future.command(101,True,'/resume',routed=True)
        self.assertTrue(future.get('paused'))
        now=time.time()
        with spot.shared.transaction():
            spot.shared._set('spot','quote',{'price':'100','time':now})
            spot.shared._set('futures','quote',{'price':'100','time':now})
        spot.shared.set_deployment_hold(False)
        self.assertFalse(spot.shared.deployment_hold())

    def test_shared_concurrent_debits_serialize_and_reject_negative_cash(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        barrier=threading.Barrier(2);results=[];errors=[]
        def run(i):
            try:
                s=SharedPaperStore(self.target)
                try:
                    barrier.wait()
                    def debit(db):
                        s._set('spot','cash',str(Decimal(s.snapshot('spot')['cash'])-Decimal('700')))
                        return 'done'
                    try:s.apply('spot','race-'+str(i),{'kind':'debit'},debit);results.append('ok')
                    except ValueError:results.append('rejected')
                finally:s.close()
            except Exception as e:errors.append(e)
        ts=[threading.Thread(target=run,args=(i,)) for i in range(2)]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertEqual(errors,[]);self.assertCountEqual(results,['ok','rejected'])
        s=SharedPaperStore(self.target)
        try:self.assertEqual(s.ledger.balance('spot:free'),Decimal(s.snapshot('spot')['cash']));self.assertTrue(s.ledger.reconcile()['balanced'])
        finally:s.close()

    def test_shared_transfer_updates_journal_and_both_wallets_once(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        s=SharedPaperStore(self.target);self.addCleanup(s.close)
        spot=Decimal(s.snapshot('spot')['cash']);futures=Decimal(s.snapshot('futures')['cash'])
        pos=s.snapshot('futures')['position']
        proposal=s.propose_transfer('telegram:1','spot','futures_isolated','50',now=1)
        event=s.confirm_transfer(proposal['id'],now=2)
        self.assertEqual(s.confirm_transfer(proposal['id'],now=3),event)
        self.assertEqual(Decimal(s.snapshot('spot')['cash']),spot-50)
        self.assertEqual(Decimal(s.snapshot('futures')['cash']),futures+50)
        self.assertEqual(s.snapshot('futures')['position'],pos)
        self.assertEqual(s.ledger.balance('spot:free'),spot-50)
        self.assertEqual(s.ledger.balance('futures_isolated:free'),futures+50)
        self.assertTrue(s.ledger.reconcile()['balanced'])

    def test_shared_transfer_rechecks_real_pause_and_fails_closed(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        s=SharedPaperStore(self.target);self.addCleanup(s.close)
        p=s.propose_transfer('one','spot','futures_isolated','50',now=1)
        with s.transaction():s._set('futures','paused',False)
        with self.assertRaises(ValueError):s.confirm_transfer(p['id'],now=2)
        self.assertEqual(s.db.execute('select status from transfer_proposals where id=?',(p['id'],)).fetchone()[0],'OPEN')
        with s.transaction():s._set('futures','paused',True)
        s.confirm_transfer(p['id'],now=3)
        self.assertTrue(s.ledger.reconcile()['balanced'])

    def test_shared_transfer_process_death_after_post_before_commit(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        s=SharedPaperStore(self.target)
        before=s.snapshot('spot')['cash'];p=s.propose_transfer('crash','spot','reserve','20',now=1);s.close()
        code=("import os,sys; from trading_bot.paper_shared_store import SharedPaperStore; "
              "s=SharedPaperStore(sys.argv[1]); s.fault=lambda p: os._exit(76) if p=='after_transfer_post' else None; "
              "s.confirm_transfer(sys.argv[2],now=2)")
        proc=subprocess.run([sys.executable,'-c',code,str(self.target),p['id']]);self.assertEqual(proc.returncode,76)
        s=SharedPaperStore(self.target);self.addCleanup(s.close)
        self.assertEqual(s.snapshot('spot')['cash'],before)
        self.assertEqual(s.ledger.balance('reserve:free'),0)
        self.assertEqual(s.db.execute('select status from transfer_proposals where id=?',(p['id'],)).fetchone()[0],'OPEN')
        s.confirm_transfer(p['id'],now=3)
        self.assertEqual(s.ledger.balance('reserve:free'),Decimal('20'))

    def test_shared_transfer_process_death_after_commit_is_exactly_once(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        s=SharedPaperStore(self.target);p=s.propose_transfer('crash-after','spot','reserve','20',now=1);s.close()
        code=("import os,sys; from trading_bot.paper_shared_store import SharedPaperStore; "
              "s=SharedPaperStore(sys.argv[1]); s.fault=lambda p: os._exit(77) if p=='after_commit' else None; "
              "s.confirm_transfer(sys.argv[2],now=2)")
        proc=subprocess.run([sys.executable,'-c',code,str(self.target),p['id']]);self.assertEqual(proc.returncode,77)
        s=SharedPaperStore(self.target);self.addCleanup(s.close)
        s.confirm_transfer(p['id'],now=3)
        self.assertEqual(s.ledger.balance('reserve:free'),Decimal('20'))
        self.assertEqual(s.db.execute("select count(*) from ledger_events where kind='TRANSFER'").fetchone()[0],1)

    def test_real_spot_reservation_and_transfer_race_cannot_overspend(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True)
        p=spot.get('position');cid=spot.prepare('SELL','.1',Decimal('100'),'clear-for-race','STOP',target=p['id']);spot.claim(cid)
        spot.apply(cid,PaperExchange.inspect_saved(spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone()));spot.close()
        s=SharedPaperStore(self.target);proposal=s.propose_transfer('race','spot','reserve','700',now=1);s.close()
        barrier=threading.Barrier(2);results=[];errors=[]
        def reserve():
            try:
                store=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True)
                try:
                    barrier.wait();store.command(100,True,'/resume',routed=True)
                    try:store.prepare('BUY','5',Decimal('100'),'race-buy','SIGNAL');results.append('reserved')
                    except Exception as exc:
                        if isinstance(exc,(ValueError,)):results.append('reserve-rejected')
                        else:
                            from trading_bot.testnet_observer import ObservationError
                            if isinstance(exc,ObservationError):results.append('reserve-rejected')
                            else:raise
                finally:store.close()
            except Exception as e:errors.append(e)
        def transfer():
            try:
                store=SharedPaperStore(self.target)
                try:
                    barrier.wait()
                    try:store.confirm_transfer(proposal['id'],now=2);results.append('transferred')
                    except ValueError:results.append('transfer-rejected')
                finally:store.close()
            except Exception as e:errors.append(e)
        ts=[threading.Thread(target=reserve),threading.Thread(target=transfer)]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertEqual(errors,[]);self.assertFalse('reserved' in results and 'transferred' in results)
        s=SharedPaperStore(self.target)
        try:self.assertTrue(s.ledger.reconcile()['balanced']);self.assertGreaterEqual(s.ledger.balance('spot:free'),0)
        finally:s.close()

    def test_real_spot_paper_fill_and_reservation_use_same_journal_transaction(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(spot.close)
        before=spot.get('position');cash=Decimal(spot.get('cash'))
        cid=spot.prepare('SELL','.1',Decimal('102'),'shared-sell','TAKE',target=before['id'])
        self.assertEqual(spot.shared.ledger.balance('spot:reserved','BTC'),Decimal('.1'))
        self.assertEqual(spot.shared.ledger.balance('spot:position','BTC'),Decimal(0))
        spot.claim(cid);row=spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone()
        report=PaperExchange.inspect_saved(row);spot.apply(cid,report);spot.apply(cid,report)
        self.assertEqual(spot.shared.ledger.balance('spot:reserved','BTC'),Decimal(0))
        self.assertEqual(spot.shared.ledger.balance('spot:position','BTC'),Decimal(0))
        self.assertEqual(spot.shared.ledger.balance('spot:free'),cash+Decimal('10.1898'))
        self.assertEqual(spot.db.execute('select count(*) from runtime_fills where client_id=?',(cid,)).fetchone()[0],1)
        self.assertTrue(spot.shared.ledger.reconcile()['balanced'])

    def test_real_futures_funding_and_close_use_same_journal_transaction(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        future=FuturesStore(self.target,shared=True);self.addCleanup(future.close)
        pos=future.get('position');oldfund=Decimal(pos['funding'])
        future.apply_funding(pos['id'],[{'time':2500,'rate':'.001','mark':'100'}],3000)
        self.assertGreater(Decimal(future.get('position')['funding']),oldfund)
        self.assertEqual(future.shared.ledger.balance('futures_isolated:funding'),Decimal(future.get('position')['funding']))
        records=[json.loads(r[0]) for r in future.db.execute("select details from ledger_events where kind='FINANCIAL_CHANGE'")]
        self.assertTrue(any(any(x['ts']==2500 for x in d['details'].get('new_funding',[])) for d in records))
        self.assertTrue(future.close_position(Decimal(98),3000,'exit-shared','STOP',pos['id']))
        self.assertFalse(future.close_position(Decimal(98),3000,'exit-shared','STOP',pos['id']))
        self.assertIsNone(future.get('position'))
        self.assertEqual(future.shared.ledger.balance('futures_isolated:margin'),0)
        self.assertEqual(future.shared.ledger.balance('futures_isolated:funding'),0)
        self.assertEqual(future.shared.ledger.balance('futures_isolated:free'),Decimal(future.get('cash')))
        self.assertTrue(future.shared.ledger.reconcile()['balanced'])

    def test_unvalued_third_asset_fee_is_journaled_and_blocks_transfers(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(spot.close)
        pos=spot.get('position');cid=spot.prepare('SELL','.1',Decimal('102'),'bnb-fee','TAKE',target=pos['id'])
        spot.claim(cid);row=spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone()
        report=PaperExchange.inspect_saved(row)
        report['fills'][0].update(commissionAsset='BNB',commission='.0001')
        spot.apply(cid,report)
        self.assertFalse(spot.get('position')['fees_known'])
        self.assertEqual(spot.shared.ledger.balance('spot:unvalued_fee','BNB'),Decimal('-.0001'))
        self.assertTrue(spot.shared.ledger.reconcile()['balanced'])
        with self.assertRaises(ValueError):spot.shared.propose_transfer('fee-unknown','spot','reserve','1',now=1)

    def test_partial_spot_fill_retains_remaining_btc_reserve(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(spot.close)
        pos=spot.get('position');cid=spot.prepare('SELL','.1',Decimal('102'),'partial','TAKE',target=pos['id']);spot.claim(cid)
        row=spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone();report=PaperExchange.inspect_saved(row)
        report['order'].update(status='PARTIALLY_FILLED',executedQty='.04',cummulativeQuoteQty='4.08')
        report['fills'][0].update(qty='.04',quoteQty='4.08',commission='.00408')
        spot.apply(cid,report);spot.apply(cid,report)
        self.assertEqual(spot.shared.ledger.balance('spot:reserved','BTC'),Decimal('.06'))
        self.assertEqual(spot.shared.ledger.balance('spot:position','BTC'),0)
        self.assertEqual(Decimal(spot.get('position')['quantity']),Decimal('.06'))
        self.assertTrue(spot.shared.ledger.reconcile()['balanced'])
        with self.assertRaises(ValueError):spot.shared.propose_transfer('pending','spot','reserve','1',now=1)

    def test_futures_margin_and_model_liquidation_are_journaled(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        future=FuturesStore(self.target,shared=True);self.addCleanup(future.close)
        pos=future.get('position')
        future.apply_funding(pos['id'],[],3000)
        self.assertTrue(future.close_position(Decimal('10000'),3000,'liquidation','MODEL_LIQUIDATION',pos['id']))
        self.assertEqual(future.shared.ledger.balance('futures_isolated:margin'),0)
        self.assertEqual(future.shared.ledger.balance('futures_isolated:entry_fee'),0)
        self.assertEqual(future.shared.ledger.balance('futures_isolated:free'),Decimal(future.get('cash')))
        self.assertTrue(future.shared.ledger.reconcile()['balanced'])

    def test_actual_futures_tick_model_liquidation_uses_journal(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        future=FuturesStore(self.target,shared=True);self.addCleanup(future.close)
        class Reader:
            def mark(self):return Decimal('10000'),3000
            def funding(self,*args):return []
            def candles(self,*args):raise AssertionError('Liquidation must precede candles')
        app=FuturesApplication(future,Reader());app.tick()
        self.assertIsNone(future.get('position'))
        row=future.db.execute("select payload from futures_events where event='CLOSE' order by id desc limit 1").fetchone()
        self.assertEqual(json.loads(row[0])['reason'],'MODEL_LIQUIDATION')
        self.assertEqual(future.shared.ledger.balance('futures_isolated:margin'),0)
        self.assertTrue(future.shared.ledger.reconcile()['balanced'])

    def test_shared_relay_reads_only_futures_outbox(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        relay=FuturesOutboxRelay(self.target);self.addCleanup(relay.close)
        sent=[]
        t=TelegramTransport('FAKE','1',lambda req:(sent.append(json.loads(req.data)['text']) or {'ok':True,'result':{}}))
        with sqlite3.connect(self.target) as db:
            spot_before=db.execute('select count(*) from runtime_outbox').fetchone()[0]
            futures_before=db.execute('select count(*) from futures_outbox').fetchone()[0]
        t.flush(relay)
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('select count(*) from runtime_outbox').fetchone()[0],spot_before)
            self.assertLess(db.execute('select count(*) from futures_outbox').fetchone()[0],futures_before)
        self.assertTrue(all('[Futures paper]' in text for text in sent))

    def test_spot_real_fill_process_death_before_and_after_commit(self):
        for point in ('before_commit','after_commit'):
            target=self.target.with_name(point+'.db')
            consolidate_snapshots(self.spot,self.future,target,captured_at=1)
            spot=RuntimeStore(target,{'mode':'paper','interval':'15m'},shared=True)
            pos=spot.get('position');cid=spot.prepare('SELL','.1',Decimal('102'),'crash-sell','TAKE',target=pos['id']);spot.claim(cid)
            old=spot.get('cash');spot.close()
            code=("import os,sys; from trading_bot.runtime_store import RuntimeStore; from trading_bot.runtime_exchange import PaperExchange; "
                  "s=RuntimeStore(sys.argv[1],{'mode':'paper','interval':'15m'},shared=True); "
                  "r=s.db.execute('select * from runtime_orders where client_id=?',(sys.argv[2],)).fetchone(); "
                  "s.fault=lambda p: os._exit(81) if p==sys.argv[3] else None; s.apply(sys.argv[2],PaperExchange.inspect_saved(r))")
            result=subprocess.run([sys.executable,'-c',code,str(target),cid,point]);self.assertEqual(result.returncode,81)
            spot=RuntimeStore(target,{'mode':'paper','interval':'15m'},shared=True)
            try:
                self.assertEqual(spot.shared.ledger.balance('spot:free'),Decimal(spot.get('cash')))
                n=spot.db.execute('select count(*) from runtime_fills where client_id=?',(cid,)).fetchone()[0]
                self.assertEqual(n,0 if point=='before_commit' else 1)
                row=spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone()
                spot.apply(cid,PaperExchange.inspect_saved(row));spot.apply(cid,PaperExchange.inspect_saved(row))
                self.assertEqual(spot.db.execute('select count(*) from runtime_fills where client_id=?',(cid,)).fetchone()[0],1)
                self.assertEqual(spot.shared.ledger.balance('spot:free'),Decimal(spot.get('cash')))
                self.assertTrue(spot.shared.ledger.reconcile()['balanced'])
            finally:spot.close()

    def test_futures_liquidation_process_death_before_and_after_commit(self):
        for point in ('before_commit','after_commit'):
            target=self.target.with_name('futures-'+point+'.db')
            consolidate_snapshots(self.spot,self.future,target,captured_at=1)
            code=("import os,sys; from decimal import Decimal; from trading_bot.futures_store import FuturesStore; "
                  "s=FuturesStore(sys.argv[1],shared=True); p=s.get('position'); "
                  "s.fault=lambda x: os._exit(82) if x==sys.argv[2] else None; "
                  "s.close_position(Decimal('10000'),2000,'liquidate-crash','MODEL_LIQUIDATION',p['id'])")
            result=subprocess.run([sys.executable,'-c',code,str(target),point]);self.assertEqual(result.returncode,82)
            future=FuturesStore(target,shared=True)
            try:
                pos=future.get('position')
                self.assertEqual(pos is None,point=='after_commit')
                self.assertEqual(future.shared.ledger.balance('futures_isolated:free'),Decimal(future.get('cash')))
                self.assertTrue(future.shared.ledger.reconcile()['balanced'])
                if pos:self.assertTrue(future.close_position(Decimal('10000'),2000,'liquidate-crash','MODEL_LIQUIDATION',pos['id']))
                self.assertEqual(future.db.execute("select count(*) from futures_events where business_key='liquidate-crash'").fetchone()[0],1)
            finally:future.close()

    def test_buy_reserves_cash_then_release_and_actual_fee(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(spot.close)
        p=spot.get('position');cid=spot.prepare('SELL','.1',Decimal('100'),'clear-pos','STOP',target=p['id']);spot.claim(cid)
        row=spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone();spot.apply(cid,PaperExchange.inspect_saved(row))
        spot.command(20,True,'/resume',routed=True)
        before=Decimal(spot.get('cash'));cid=spot.prepare('BUY','.05',Decimal('100'),'buy-again','SIGNAL')
        self.assertEqual(spot.shared.ledger.balance('spot:reserved'),Decimal('5.0100'))
        self.assertEqual(spot.shared.ledger.balance('spot:free'),before-Decimal('5.0100'))
        spot.claim(cid);row=spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone();spot.apply(cid,PaperExchange.inspect_saved(row))
        self.assertEqual(spot.shared.ledger.balance('spot:reserved'),0)
        self.assertEqual(spot.shared.ledger.balance('spot:free'),before-Decimal('5.005'))
        self.assertEqual(spot.shared.ledger.balance('spot:position','BTC'),Decimal('.05'))
        self.assertTrue(spot.shared.ledger.reconcile()['balanced'])

    def test_partial_buy_preserves_unfilled_cash_reserve(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        spot=RuntimeStore(self.target,{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(spot.close)
        pos=spot.get('position');cid=spot.prepare('SELL','.1',Decimal('100'),'clear-for-partial-buy','STOP',target=pos['id']);spot.claim(cid)
        spot.apply(cid,PaperExchange.inspect_saved(spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone()))
        spot.command(101,True,'/resume',routed=True);before=Decimal(spot.get('cash'))
        cid=spot.prepare('BUY','.05',Decimal('100'),'partial-buy','SIGNAL');spot.claim(cid)
        report=PaperExchange.inspect_saved(spot.db.execute('select * from runtime_orders where client_id=?',(cid,)).fetchone())
        report['order'].update(status='PARTIALLY_FILLED',executedQty='.02',cummulativeQuoteQty='2')
        report['fills'][0].update(qty='.02',quoteQty='2',commission='.002')
        spot.apply(cid,report);spot.apply(cid,report)
        self.assertEqual(spot.shared.ledger.balance('spot:reserved'),Decimal('3.008'))
        self.assertEqual(spot.shared.ledger.balance('spot:free'),before-Decimal('5.01'))
        self.assertEqual(spot.shared.ledger.balance('spot:position','BTC'),Decimal('.02'))
        self.assertTrue(spot.shared.ledger.reconcile()['balanced'])

    def test_futures_open_journals_margin_and_entry_fee(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        future=FuturesStore(self.target,shared=True);self.addCleanup(future.close)
        p=future.get('position');future.close_position(Decimal(100),2000,'close-first','STOP',p['id'])
        future.command(20,True,'/resume',routed=True)
        before=Decimal(future.get('cash'))
        self.assertTrue(future.open_position('LONG',Decimal(100),3000,'open-next'))
        p=future.get('position')
        self.assertEqual(future.shared.ledger.balance('futures_isolated:free'),before-Decimal('10'))
        self.assertEqual(future.shared.ledger.balance('futures_isolated:margin'),Decimal('10'))
        self.assertEqual(future.shared.ledger.balance('futures_isolated:entry_fee'),-Decimal(p['entry_fee']))
        self.assertTrue(future.shared.ledger.reconcile()['balanced'])

    def test_shared_futures_arithmetic_matches_legacy_decimal_precision(self):
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=1)
        shared=FuturesStore(self.target,shared=True);legacy=FuturesStore(self.future)
        self.addCleanup(shared.close);self.addCleanup(legacy.close)
        for store in (shared,legacy):
            pos=store.get('position');store.close_position(Decimal('101.23456789'),2000,'compare-close','STOP',pos['id'])
            store.command(20,True,'/resume',routed=True)
            store.open_position('LONG',Decimal('99.12345678'),3000,'compare-open')
        self.assertEqual(shared.get('cash'),legacy.get('cash'))
        for field in ('entry','quantity','entry_fee','margin','leverage','funding'):
            self.assertEqual(shared.get('position')[field],legacy.get('position')[field],field)
