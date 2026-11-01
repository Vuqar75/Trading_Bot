import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_paper_strategy import CrossPaperStrategy
from trading_bot.cross_funding_tracker import CrossFundingTracker
from trading_bot.cross_data_sync import CrossDataSync
from trading_bot.cross_command_receiver import CrossCommandReceiver
from trading_bot.cross_worker import process_cross
from trading_bot.cross_app import cycle
from trading_bot.cross_trade_report import cross_trade_report
from trading_bot.cross_report_worker import process_cross_reports
from trading_bot.paper_wallet_live_report import live_report, render_live_report
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.app import TelegramTransport


class CrossPaperBookTests(unittest.TestCase):
    @staticmethod
    def quote(mark, stamp, complete=True):
        return {'mark':mark,'time':stamp,'funding_complete':complete,'server_ms':int(stamp*1000),'next_due_ms':28000000,'interval_ms':28800000}

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
        self.quotes={'BTCUSDT':self.quote('100',100),'ETHUSDT':self.quote('200',100)}
        self.book.record_quotes(self.quotes,now=100)
        self.book.set_paused(False,now=100)

    def positions(self):
        btc=self.book.open('btc','BTCUSDT','LONG','5',1,self.quotes,now=100)
        eth=self.book.open('eth','ETHUSDT','SHORT','5',1,self.quotes,now=100)
        return btc,eth

    def test_opposite_results_affect_same_equity_and_available(self):
        btc,eth=self.positions()
        changed={'BTCUSDT':self.quote('80',101),'ETHUSDT':self.quote('180',101)}
        base=self.book.risk(self.quotes,now=100);self.book.record_quotes(changed,now=101);risk=self.book.risk(changed,now=101)
        self.assertLess(risk['equity'],base['equity']);self.assertLess(risk['available'],base['available'])
        self.assertEqual(risk['margin'],Decimal('10'));self.assertEqual(len(risk['positions']),2)
        self.assertEqual(self.book.open('btc','BTCUSDT','LONG','5',1,self.quotes,now=100),btc)
        self.assertEqual(self.shared.ledger.balance('spot:free'),Decimal('1000'))

    def test_balance_report_counts_cross_capital_once_and_marks_unknown(self):
        self.positions();risk=self.book.risk(self.quotes,now=100)
        data=live_report(self.shared,now=100,period='all')
        with localcontext() as ctx:
            ctx.prec=120
            self.assertEqual(data['total'],Decimal('1950')+risk['equity']-risk['exit_cost'])
        self.assertEqual(data['cross']['margin'],Decimal(10))
        self.assertIn('Futures cross • PAPER',render_live_report(data))
        self.book.invalidate_data('ETHUSDT')
        incomplete=live_report(self.shared,now=100,period='today')
        self.assertIsNone(incomplete['total']);self.assertIsNone(incomplete['cross']['gross'])
        self.assertIn('неполны',render_live_report(incomplete))

    def test_cross_trade_report_has_baku_day_and_cumulative(self):
        btc,_=self.positions()
        quotes={'BTCUSDT':self.quote('105',101),'ETHUSDT':self.quote('200',101)}
        self.book.record_quotes(quotes,now=101)
        self.book.close('close-btc','BTCUSDT',btc['id'],btc['quantity'],quotes,now=101)
        with self.shared.transaction():
            self.shared.ledger._post('old-open','CROSS_OPEN',{'id':'old-position'},
                [('futures_cross:free','USDT','-1'),('external:cross_fees','USDT','1')],created=-90000)
            self.shared.ledger._post('old-close','CROSS_CLOSE',{'position_id':'old-position','gross':'3','exit_fee':'1','funding':'0'},
                [('futures_cross:free','USDT','2'),('external:cross_pnl','USDT','-3'),('external:cross_fees','USDT','1')],created=-89999)
        text=cross_trade_report(self.shared,now=102)
        self.assertIn('Сегодня по Баку',text);self.assertIn('За всё время',text)
        self.assertIn('закрыто 1',text);self.assertIn('закрыто 2',text)
        self.assertIn('комиссии',text);self.assertIn('funding',text)

    def test_cross_report_dispatch_keeps_mode_and_balance_report_global(self):
        q=UnifiedQueue(Path(self.tmp.name)/'reports.db');self.addCleanup(q.close)
        q.receive(1,True,'/mode futures_cross');q.receive(2,True,'/report')
        q.receive(3,True,'/mode spot');q.receive(4,True,'/balance_report today')
        process_cross_reports(q,self.shared,now=100)
        self.assertFalse(q.pending('futures_cross'))
        self.assertEqual(q.pending('wallet')[0]['command'],'/balance_report today')
        out=[r[0] for r in q.db.execute('SELECT message FROM unified_outbox')]
        self.assertTrue(any('Futures cross • PAPER' in message and 'За всё время' in message for message in out))

    def test_cross_report_does_not_pass_pending_pause(self):
        q=UnifiedQueue(Path(self.tmp.name)/'ordered-reports.db');self.addCleanup(q.close)
        q.receive(1,True,'/mode futures_cross');q.receive(2,True,'/pause');q.receive(3,True,'/report')
        process_cross_reports(q,self.shared,now=100)
        self.assertEqual([j['command'] for j in q.pending('futures_cross')],['/pause','/report'])

    def test_cross_close_choice_confirm_and_crash_do_not_close_twice(self):
        btc,eth=self.positions();q=UnifiedQueue(Path(self.tmp.name)/'cross-commands.db');self.addCleanup(q.close)
        receiver=CrossCommandReceiver(self.book)
        snapshots={'futures_cross':{'BTCUSDT':btc,'ETHUSDT':eth}}
        q.receive(1,True,'/mode futures_cross',now=100)
        q.receive(2,True,'/close',positions=snapshots,now=100)
        self.assertFalse(q.pending('futures_cross'))
        q.receive(3,True,'/close BTCUSDT',positions=snapshots,now=100)
        process_cross(q,receiver,self.book,now=100,clock=lambda:100)
        token=q.db.execute("SELECT id FROM unified_proposals WHERE market='futures_cross'").fetchone()[0]
        self.assertTrue(token.startswith('BTCUSDT-'))
        q.receive(4,True,'/mode spot',now=100);q.receive(5,True,'/confirm '+token,now=101)
        def crash(point):
            if point=='after_destination_commit':raise RuntimeError('injected crash')
        with self.assertRaises(RuntimeError):process_cross(q,receiver,self.book,now=101,clock=lambda:101,fault=crash)
        self.assertEqual(len(q.pending('futures_cross')),1)
        process_cross(q,receiver,self.book,started=102,now=102,clock=lambda:102)
        self.assertFalse(q.pending('futures_cross'))
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='CROSS_CLOSE'").fetchone()[0],1)
        messages=[r[0] for r in self.shared.db.execute("SELECT message FROM futures_outbox WHERE message LIKE '[Futures cross%'")]
        self.assertEqual(sum('Закрыта BTCUSDT' in x for x in messages),1)
        self.assertEqual([p['symbol'] for p in self.book.risk(self.quotes,now=101)['positions']],['ETHUSDT'])

    def test_real_poller_freezes_cross_position_and_symbol_at_receipt(self):
        btc,_=self.positions();q=UnifiedQueue(Path(self.tmp.name)/'cross-poller.db');self.addCleanup(q.close)
        store=RuntimeStore(self.path,{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(store.close)
        now=int(__import__('time').time());q.receive(1,True,'/mode futures_cross')
        update={'update_id':2,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'date':now,'text':'/close BTCUSDT'}}
        TelegramTransport('FAKE','1',lambda req:{'ok':True,'result':[update]},started=now-1).poll(store,q)
        payload=json.loads(q.pending('futures_cross')[0]['payload'])
        self.assertEqual((payload['symbol'],payload['position'],payload['quantity']),('BTCUSDT',btc['id'],btc['quantity']))
        q.receive(3,True,'/mode spot')
        self.assertEqual(q.pending('futures_cross')[0]['command'],'/close')

    def test_cross_close_expires_inside_transaction_without_trade(self):
        btc,_=self.positions();q=UnifiedQueue(Path(self.tmp.name)/'expiring-cross.db');self.addCleanup(q.close)
        receiver=CrossCommandReceiver(self.book);snap={'futures_cross':{'BTCUSDT':btc}}
        q.receive(1,True,'/mode futures_cross',now=100);q.receive(2,True,'/close',positions=snap,now=100)
        process_cross(q,receiver,self.book,now=100,clock=lambda:100)
        token=q.db.execute('SELECT id FROM unified_proposals').fetchone()[0]
        q.receive(3,True,'/confirm '+token,now=101)
        process_cross(q,receiver,self.book,now=101,clock=lambda:221)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='CROSS_CLOSE'").fetchone()[0],0)

    def test_pause_all_waits_for_cross_and_replays_after_crash(self):
        q=UnifiedQueue(Path(self.tmp.name)/'pause-all-cross.db');self.addCleanup(q.close)
        receiver=CrossCommandReceiver(self.book);q.receive(1,True,'/pause_all',now=100)
        for market in ('spot','futures'):q.acknowledge(q.pending(market)[0]['id'],'на паузе')
        def crash(point):
            if point=='after_destination_commit':raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):process_cross(q,receiver,self.book,now=100,clock=lambda:100,fault=crash)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        process_cross(q,receiver,self.book,now=100,clock=lambda:100)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_commands').fetchone()[0],1)
        self.assertTrue(any('применены ко всем трём' in r[0] for r in q.db.execute('SELECT message FROM unified_outbox')))

    def test_stale_or_incomplete_pair_and_pause_block_entry(self):
        self.book.set_paused(True)
        with self.assertRaisesRegex(ValueError,'paused'):self.book.open('a','BTCUSDT','LONG','5',1,self.quotes,now=100)
        self.book.set_paused(False,now=100)
        with self.assertRaisesRegex(ValueError,'Stale'):self.book.open('a','BTCUSDT','LONG','5',1,self.quotes,now=221)
        bad={**self.quotes,'ETHUSDT':self.quote('200',100,False)}
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.book.open('a','BTCUSDT','LONG','5',1,bad,now=100)
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal('50'))

    def test_deployment_hold_blocks_cross_entries_even_if_previously_unpaused(self):
        self.shared.set_deployment_hold(True)
        with self.assertRaisesRegex(ValueError,'hold'):self.book.open('held','BTCUSDT','LONG','5',1,self.quotes,now=100)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_positions').fetchone()[0],0)

    def test_funding_deduplication_and_explicit_revision(self):
        btc,eth=self.positions()
        x=self.book.funding('fund-1','BTCUSDT',btc['id'],28000000,0,'.001','100','fixture',now=28000)
        self.assertEqual(self.book.funding('fund-1','BTCUSDT',btc['id'],28000000,0,'.001','100','fixture',now=28001),x)
        self.assertLess(Decimal(x['value']),0)
        y=self.book.funding('fund-2','ETHUSDT',eth['id'],28000000,0,'.001','200','fixture',now=28000)
        self.assertGreater(Decimal(y['value']),0)
        correction=self.book.funding('fund-1-correction','BTCUSDT',btc['id'],28000000,1,'.002','100','corrected fixture',now=28000)
        self.assertEqual(Decimal(correction['delta']),Decimal(x['value']))
        special=self.book.funding('fund-special','BTCUSDT',btc['id'],28000000,0,'.003','100','fixture',now=28000,rate_type='Special')
        self.assertLess(Decimal(special['value']),0)
        with self.assertRaisesRegex(ValueError,'revision'):self.book.funding('another','BTCUSDT',btc['id'],28000000,1,'.002','100','fixture',now=28000)
        with self.assertRaisesRegex(ValueError,'lifetime'):self.book.funding('too-early','BTCUSDT',btc['id'],99999,0,'.001','100','fixture',now=100)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_funding_tracker_requires_persisted_events_and_replays_after_commit(self):
        btc,_=self.positions();tracker=CrossFundingTracker(self.book)
        tracker.start(btc['id'],'BTCUSDT',28000000,28800000,100000)
        event={'symbol':'BTCUSDT','time':28000000,'rate_type':'Regular','rate':'.001','mark':'100'}
        args={'expected_due':28000000,'server_ms':28001000,'observed_due':56800000,'observed_interval':28800000,'events':[event]}
        with self.assertRaisesRegex(ValueError,'Invalidate'):tracker.confirm(btc['id'],**args)
        self.book.invalidate_all()
        with self.assertRaisesRegex(ValueError,'not fully journaled'):tracker.confirm(btc['id'],**args)
        self.book.funding('settlement','BTCUSDT',btc['id'],28000000,0,'.001','100','fixture',now=28001)
        def crash_before(point):
            if point=='before_commit':raise RuntimeError('simulated process death before commit')
        self.shared.fault=crash_before
        with self.assertRaises(RuntimeError):tracker.confirm(btc['id'],**args)
        self.shared.fault=lambda point:None
        self.assertEqual(self.shared.db.execute('SELECT next_due FROM cross_funding_cursor WHERE position_id=?',(btc['id'],)).fetchone()[0],28000000)
        def crash(point):
            if point=='after_commit':raise RuntimeError('simulated process death')
        self.shared.fault=crash
        with self.assertRaises(RuntimeError):tracker.confirm(btc['id'],**args)
        self.shared.fault=lambda point:None
        self.assertEqual(tracker.confirm(btc['id'],**args)['next_due'],56800000)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_coverage_receipts').fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('UPDATE cross_funding_cursor SET next_due=999999 WHERE position_id=?',(btc['id'],))
        with self.assertRaisesRegex(ValueError,'cursor journal mismatch'):tracker.confirm(btc['id'],**args)

    def test_funding_deadline_blocks_fresh_quotes_entries_and_withdrawal(self):
        self.book.open('btc','BTCUSDT','LONG','5',1,self.quotes,now=100)
        near={'BTCUSDT':self.quote('100',27999),'ETHUSDT':self.quote('200',27999)}
        self.book.record_quotes(near,now=27999)
        with self.assertRaisesRegex(ValueError,'coverage requires'):
            self.book.open('after-due','ETHUSDT','SHORT','5',1,near,now=28000)
        self.book.set_paused(True)
        with self.assertRaisesRegex(ValueError,'coverage requires'):
            self.shared.propose_transfer('after-due-transfer','futures_cross','reserve','1',now=28000)
        advanced={s:{**q,'time':28001,'server_ms':28001000,'next_due_ms':56800000} for s,q in near.items()}
        with self.assertRaisesRegex(ValueError,'coverage requires'):self.book.record_quotes(advanced,now=28001)

    def test_both_funding_cursors_must_advance_before_quote_publication(self):
        btc,eth=self.positions();tracker=CrossFundingTracker(self.book);self.book.invalidate_all()
        next_quotes={s:{**q,'time':28001,'server_ms':28001000,'next_due_ms':56800000} for s,q in self.quotes.items()}
        for pos,symbol,mark in [(btc,'BTCUSDT','100'),(eth,'ETHUSDT','200')]:
            event={'symbol':symbol,'time':28000000,'rate_type':'Regular','rate':'.001','mark':mark}
            self.book.funding('settlement:'+symbol,symbol,pos['id'],28000000,0,'.001',mark,'fixture',now=28001)
            tracker.confirm(pos['id'],expected_due=28000000,server_ms=28001000,observed_due=56800000,
                            observed_interval=28800000,events=[event])
            if symbol=='BTCUSDT':
                with self.assertRaisesRegex(ValueError,'coverage requires'):self.book.record_quotes(next_quotes,now=28001)
        self.book.record_quotes(next_quotes,now=28001)
        replay={'symbol':'BTCUSDT','time':28000000,'rate_type':'Regular','rate':'.001','mark':'100'}
        tracker.confirm(btc['id'],expected_due=28000000,server_ms=28001001,observed_due=56800000,
                        observed_interval=28800000,events=[replay])
        self.assertEqual(len(self.book.risk(next_quotes,now=28001)['positions']),2)
        correction=self.book.funding('corrected:btc','BTCUSDT',btc['id'],28000000,1,'.002','100','fixture',now=28002)
        self.assertLess(Decimal(correction['delta']),0)
        corrected={'symbol':'BTCUSDT','time':28000000,'rate_type':'Regular','revision':1,'rate':'.002','mark':'100'}
        refreshed={s:{**q,'time':28002,'server_ms':28002000} for s,q in next_quotes.items()}
        with self.assertRaisesRegex(ValueError,'correction requires'):self.book.record_quotes(refreshed,now=28002)
        tracker.confirm_correction(btc['id'],corrected);tracker.confirm_correction(btc['id'],corrected)
        self.book.record_quotes(refreshed,now=28002)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_correction_receipts').fetchone()[0],3)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_data_sync_recovers_after_one_pair_missing_without_double_funding(self):
        self.positions()
        class Reader:
            missing=True
            def schedule(self,symbol):
                return {'symbol':symbol,'mark':'100' if symbol=='BTCUSDT' else '200','server_ms':28001000,
                        'next_due_ms':56800000,'interval_ms':28800000}
            def funding(self,symbol,start,end):
                if self.missing and symbol=='ETHUSDT':return []
                return [{'symbol':symbol,'time':28000000,'rate_type':'Regular','rate':'.001','mark':'100' if symbol=='BTCUSDT' else '200'}]
        reader=Reader();sync=CrossDataSync(self.book,reader)
        with self.assertRaisesRegex(ValueError,'not published'):sync.refresh(now=28001)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_funding').fetchone()[0],1)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_quotes WHERE funding_complete!=0').fetchone()[0],0)
        reader.missing=False;quotes=sync.refresh(now=28001)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_funding').fetchone()[0],2)
        self.assertEqual(len(self.book.risk(quotes,now=28001)['positions']),2)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_cross_cycle_fake_market_opens_both_and_pauses_on_data_failure(self):
        period=900000;server=31*period+1000;now=server/1000
        q=UnifiedQueue(Path(self.tmp.name)/'cycle-queue.db');self.addCleanup(q.close)
        receiver=CrossCommandReceiver(self.book);strategy=CrossPaperStrategy(self.book)
        class Reader:
            broken=False
            def schedule(self,symbol):
                if self.broken:raise RuntimeError('data unavailable')
                return {'symbol':symbol,'mark':'100' if symbol=='BTCUSDT' else '200','server_ms':server,
                        'next_due_ms':28000000,'interval_ms':28800000}
            def funding(self,symbol,start,end):return []
            def candles(self,symbol,interval,server):
                last='101' if symbol=='BTCUSDT' else '99'
                return [(i*period,'100') for i in range(30)]+[(30*period,last)]
        reader=Reader();sync=CrossDataSync(self.book,reader)
        actions=cycle(self.book,q,receiver,strategy,sync,reader,started=0,now=now)
        self.assertEqual([a['action'] for a in actions],['OPEN','OPEN'])
        self.assertEqual(cycle(self.book,q,receiver,strategy,sync,reader,started=0,now=now),[])
        reader.broken=True
        with self.assertRaises(RuntimeError):cycle(self.book,q,receiver,strategy,sync,reader,started=0,now=now)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_quotes WHERE funding_complete!=0').fetchone()[0],0)

    def test_cross_process_once_starts_paused_with_network_blocked(self):
        queue=Path(self.tmp.name)/'process-queue.db';q=UnifiedQueue(queue);q.close()
        code='''import json,socket,sys,time
from unittest.mock import patch
import trading_bot.cross_app as app
class Fake:
 def schedule(self,symbol):
  now=int(time.time()*1000);return {'symbol':symbol,'mark':'100','server_ms':now,'next_due_ms':now+3600000,'interval_ms':28800000}
 def funding(self,*args):return []
 def candles(self,symbol,interval,server):
  period=900000;last=(server//period-1)*period
  return [(last-(39-i)*period,'100') for i in range(40)]
def deny(*args,**kwargs):raise AssertionError('network prohibited')
with patch.object(socket,'socket',deny),patch.object(app,'CrossPublicData',Fake):
 sys.argv=['cross-app','--db',sys.argv[1],'--queue',sys.argv[2],'--once'];app.main()
'''
        child=subprocess.run([sys.executable,'-c',code,str(self.path),str(queue)],capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr)
        health=json.loads((self.path.parent/'cross-health.json').read_text())
        self.assertTrue(health['paused']);self.assertTrue(health['data_complete']);self.assertIsNone(health['error'])
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_positions').fetchone()[0],0)

    def test_late_funding_debt_is_explicit_and_only_cross_cash_settles_it(self):
        btc,eth=self.positions();tracker=CrossFundingTracker(self.book);self.book.invalidate_all()
        for pos,symbol,mark in [(btc,'BTCUSDT','100'),(eth,'ETHUSDT','200')]:
            event={'symbol':symbol,'time':28000000,'rate_type':'Regular','rate':'.001','mark':mark}
            self.book.funding('settle:'+symbol,symbol,pos['id'],28000000,0,'.001',mark,'fixture',now=28001)
            tracker.confirm(pos['id'],expected_due=28000000,server_ms=28001000,observed_due=56800000,
                            observed_interval=28800000,events=[event])
        quotes={s:{**q,'time':28001,'server_ms':28001000,'next_due_ms':56800000} for s,q in self.quotes.items()}
        self.book.record_quotes(quotes,now=28001)
        self.book.close('close-before-correction','BTCUSDT',btc['id'],btc['quantity'],quotes,now=28001)
        before_isolated=self.shared.ledger.balance('futures_isolated:free')
        result=self.book.late_funding('late','BTCUSDT',btc['id'],28000000,1,'.9','10000','fixture',now=28002)
        self.assertLess(Decimal(result['liability']),0)
        self.assertEqual(self.book.late_funding('late','BTCUSDT',btc['id'],28000000,1,'.9','10000','fixture',now=28003),result)
        self.assertEqual(self.shared.ledger.balance('spot:free'),Decimal('1000'))
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),before_isolated)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        before_debt=self.shared.ledger.balance('futures_cross:liability')
        proposal=self.shared.propose_transfer('cover-debt','futures_isolated','futures_cross','1',now=28002)
        self.assertEqual(Decimal(proposal['destination_after']),0)
        self.shared.confirm_transfer(proposal['id'],now=28002)
        with localcontext() as ctx:
            ctx.prec=120
            self.assertEqual(self.shared.ledger.balance('futures_cross:liability'),before_debt+1)
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),before_isolated-1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_data_sync_detects_correction_after_position_closed(self):
        btc,eth=self.positions();tracker=CrossFundingTracker(self.book);self.book.invalidate_all()
        for pos,symbol,mark in [(btc,'BTCUSDT','100'),(eth,'ETHUSDT','200')]:
            event={'symbol':symbol,'time':28000000,'rate_type':'Regular','rate':'.001','mark':mark}
            self.book.funding('initial:'+symbol,symbol,pos['id'],28000000,0,'.001',mark,'fixture',now=28001)
            tracker.confirm(pos['id'],expected_due=28000000,server_ms=28001000,observed_due=56800000,
                            observed_interval=28800000,events=[event])
        quotes={s:{**q,'time':28001,'server_ms':28001000,'next_due_ms':56800000} for s,q in self.quotes.items()}
        self.book.record_quotes(quotes,now=28001)
        self.book.close('closed-before-revision','BTCUSDT',btc['id'],btc['quantity'],quotes,now=28001)
        class Reader:
            def schedule(self,symbol):
                return {'symbol':symbol,'mark':'100' if symbol=='BTCUSDT' else '200','server_ms':28002000,
                        'next_due_ms':56800000,'interval_ms':28800000}
            def funding(self,symbol,start,end):
                return [{'symbol':symbol,'time':28000000,'rate_type':'Regular','rate':'.002' if symbol=='BTCUSDT' else '.001',
                         'mark':'100' if symbol=='BTCUSDT' else '200'}]
        fresh=CrossDataSync(self.book,Reader()).refresh(now=28002)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='CROSS_LATE_FUNDING'").fetchone()[0],1)
        self.assertEqual(len(self.book.risk(fresh,now=28002)['positions']),1)
        CrossDataSync(self.book,Reader()).refresh(now=28002)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='CROSS_LATE_FUNDING'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_shared_liquidation_records_deficit_and_preserves_other_wallets(self):
        self.positions()
        # Synthetic catastrophic price move exercises the explicit model write-off.
        mark={'BTCUSDT':self.quote('1',101),'ETHUSDT':self.quote('4000',101)}
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
        with closing(sqlite3.connect(self.path)) as db,db:db.execute("UPDATE cross_positions SET entry='999' WHERE symbol='BTCUSDT'")
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

    def test_process_death_around_funding_commit_keeps_quotes_blocked(self):
        btc,_=self.positions();self.book.invalidate_all()
        code=("import os,sys; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from trading_bot.cross_paper_book import CrossPaperBook; "
              "s=SharedPaperStore(sys.argv[1]); b=CrossPaperBook(s); "
              "s.fault=lambda p: os._exit(75) if p==sys.argv[2] else None; "
              "b.funding('crash-funding','BTCUSDT',sys.argv[3],28000000,0,'.001','100','fixture',now=28001)")
        for point in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-c',code,str(self.path),point,btc['id']])
            self.assertEqual(child.returncode,75)
            self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_funding').fetchone()[0],int(point=='after_commit'))
            self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_quotes WHERE funding_complete!=0').fetchone()[0],0)
            self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        self.book.funding('crash-funding','BTCUSDT',btc['id'],28000000,0,'.001','100','fixture',now=28002)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_funding').fetchone()[0],1)

    def test_strategy_two_signals_duplicate_tick_and_protection_while_paused(self):
        strategy=CrossPaperStrategy(self.book);period=900000;server=31*period+1000
        now=server/1000;quotes={s:{**q,'time':now,'server_ms':server} for s,q in self.quotes.items()}
        candles={symbol:[(i*period,'100') for i in range(30)]+[(30*period,last)]
                 for symbol,last in [('BTCUSDT','101'),('ETHUSDT','99')]}
        actions=strategy.tick(quotes,candles,server,now=now)
        self.assertEqual([x['action'] for x in actions],['OPEN','OPEN'])
        self.assertEqual(strategy.tick(quotes,candles,server,now=now),[])
        self.book.set_paused(True)
        high={'BTCUSDT':self.quote('105',now+1),'ETHUSDT':self.quote('200',now+1)}
        actions=strategy.tick(high,candles,server,now=now+1)
        self.assertEqual([(x['action'],x['symbol']) for x in actions],[('TAKE','BTCUSDT')])
        positions=self.book.risk(high,now=now+1)['positions']
        self.assertEqual([p['symbol'] for p in positions],['ETHUSDT'])

    def test_strategy_incomplete_market_or_future_candle_pauses_entries(self):
        strategy=CrossPaperStrategy(self.book);period=900000;server=31*period+1000
        now=server/1000;quotes={s:{**q,'time':now,'server_ms':server} for s,q in self.quotes.items()}
        candles={symbol:[(i*period,'100') for i in range(31)] for symbol in ('BTCUSDT','ETHUSDT')}
        bad={**quotes,'ETHUSDT':self.quote('200',now,False)}
        with self.assertRaisesRegex(ValueError,'Incomplete'):strategy.tick(bad,candles,server,now=now)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        self.book.record_quotes(quotes,now=now);self.book.set_paused(False,now=now)
        candles['ETHUSDT'][-1]=(32*period,'100')
        with self.assertRaises(ValueError):strategy.tick(quotes,candles,server,now=now)
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        self.assertEqual(self.shared.db.execute('SELECT count(*) FROM cross_positions').fetchone()[0],0)

    def test_strategy_death_after_open_does_not_repeat_entry(self):
        period=900000;server=31*period+1000
        now=server/1000;quotes={s:{**q,'time':now,'server_ms':server} for s,q in self.quotes.items()}
        candles={s:[(i*period,'100') for i in range(30)]+[(30*period,last)]
                 for s,last in [('BTCUSDT','101'),('ETHUSDT','99')]}
        code=("import os,sys,json; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from trading_bot.cross_paper_book import CrossPaperBook; "
              "from trading_bot.cross_paper_strategy import CrossPaperStrategy; "
              "s=SharedPaperStore(sys.argv[1]); b=CrossPaperBook(s); "
              "a=CrossPaperStrategy(b,fault=lambda p:os._exit(74) if p=='after_open_commit' else None); "
              "a.tick(json.loads(sys.argv[2]),json.loads(sys.argv[3]),int(sys.argv[4]),now=int(sys.argv[4])/1000)")
        child=subprocess.run([sys.executable,'-c',code,str(self.path),json.dumps(quotes),json.dumps(candles),str(server)])
        self.assertEqual(child.returncode,74)
        CrossPaperStrategy(self.book).tick(quotes,candles,server,now=now)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='CROSS_OPEN'").fetchone()[0],2)
        self.assertEqual(len(self.book.risk(quotes,now=now)['positions']),2)


if __name__ == '__main__':
    unittest.main()
