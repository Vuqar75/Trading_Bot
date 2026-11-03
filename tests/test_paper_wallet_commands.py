import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from decimal import Decimal,localcontext
from pathlib import Path
from trading_bot.app import Application,TelegramTransport
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report,render_live_report,_fmt
from trading_bot.paper_wallet_worker import process_wallet
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_command_receiver import MarginCommandReceiver
from trading_bot.margin_worker import process_margin
from trading_bot.margin_app import cycle
from trading_bot.margin_paper_strategy import MarginPaperStrategy
from trading_bot.margin_spot_data import MarginSpotData
from trading_bot.margin_rollback_guard import may_restore_pre_margin_code
from trading_bot.outbox_relay import FuturesOutboxRelay
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.unified_worker import process_market
from trading_bot.paper_monitor import snapshot


class WalletCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name)
        spot=RuntimeStore(p/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(p/'future.db')
        try:
            app=Application(spot,PaperExchange(),None);spot.command(1,True,'/resume');app.execute('BUY','.1',Decimal(100),'entry','TEST')
            future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),1000,'entry')
            pos=future.get('position');future.apply_funding(pos['id'],[{'time':1500,'rate':'.001','mark':'100'}],2000)
            with spot.transaction():spot.set('paused',True)
            with future.transaction():future.set('paused',True)
            for src,name in ((spot.db,'spot-copy.db'),(future.db,'future-copy.db')):
                db=sqlite3.connect(p/name)
                try:src.backup(db)
                finally:db.close()
        finally:spot.close();future.close()
        consolidate_snapshots(p/'spot-copy.db',p/'future-copy.db',p/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(p/'shared.db');self.q=UnifiedQueue(p/'queue.db')
        self.addCleanup(self.shared.close);self.addCleanup(self.q.close)
        now=time.time()
        with self.shared.transaction():
            self.shared._set('spot','quote',{'price':'100','time':now})
            self.shared._set('futures','quote',{'price':'100','time':now})

    def _process(self,**kwargs):process_wallet(self.q,self.shared,**kwargs)

    def test_relay_does_not_label_margin_or_cross_as_isolated_futures(self):
        with self.shared.transaction():
            self.shared.db.execute('DELETE FROM futures_outbox')
            for text in ('[Spot margin cross • PAPER] margin','[Futures cross • PAPER] cross','isolated'):
                self.shared.db.execute('INSERT INTO futures_outbox(message) VALUES(?)',(text,))
        sent=[]
        transport=TelegramTransport('FAKE','1',lambda req:(sent.append(json.loads(req.data)['text']) or {'ok':True,'result':{}}))
        relay=FuturesOutboxRelay(Path(self.tmp.name)/'shared.db')
        try:transport.flush(relay)
        finally:relay.close()
        self.assertEqual(sent[0],'[Spot margin cross • PAPER] margin')
        self.assertEqual(sent[1],'[Futures cross • PAPER] cross')
        self.assertEqual(sent[2],'[Futures paper]\nisolated')

    def test_margin_report_counts_assets_and_debt_once_and_marks_due_interest(self):
        book=MarginPaperBook(self.shared.ledger)
        now=time.time();ms=int(now*1000)
        filters={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':ms}
        quotes={s:{'price':price,'time_ms':ms,'server_ms':ms,'source':'fixture','price_kind':'last_public_trade','filters':filters} for s,price in [('BTCUSDT','100'),('ETHUSDT','200')]}
        book.record_quotes(quotes,now_ms=ms)
        before=live_report(self.shared,now=now)
        proposal=self.shared.propose_transfer('margin-test','futures_isolated','margin_cross','50',now=now)
        self.shared.confirm_transfer(proposal['id'],now=now)
        transferred=live_report(self.shared,now=now)
        self.assertEqual(before['total'],transferred['total']);self.assertTrue(transferred['valuation_ok'])
        with self.shared.ledger.transaction():self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        book.open('entry','p','ETHUSDT','SHORT','.04','5',2,quotes,now_ms=ms)
        report=live_report(self.shared,now=now)
        self.assertTrue(report['valuation_ok']);self.assertLess(report['total'],transferred['total'])
        self.assertEqual(report['margin_cross']['assets']['ETH']['principal'],Decimal('-.04'))
        self.assertIn('Spot margin cross',render_live_report(report))
        delayed=live_report(self.shared,now=now+3601)
        self.assertIsNone(delayed['total']);self.assertTrue(any('процент' in item for item in delayed['incomplete']))

    def test_margin_queue_close_replay_does_not_repeat_financial_action(self):
        book=MarginPaperBook(self.shared.ledger);receiver=MarginCommandReceiver(book)
        now=time.time();ms=int(now*1000);clock=lambda:now
        f={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':ms}
        quotes={s:{'price':price,'time_ms':ms,'server_ms':ms,'source':'fixture','price_kind':'last_public_trade','filters':f} for s,price in [('BTCUSDT','100'),('ETHUSDT','200')]}
        book.record_quotes(quotes,now_ms=ms)
        proposal=self.shared.propose_transfer('margin-test','futures_isolated','margin_cross','50',now=now)
        self.shared.confirm_transfer(proposal['id'],now=now)
        receiver.protection_checked_ms=ms;receiver.apply(99,'/resume',{},clock_ms=lambda:ms)
        book.open('entry','position','ETHUSDT','SHORT','.04','5',2,quotes,now_ms=ms)
        self.q.receive(1,True,'/mode margin_cross',now=now)
        self.q.receive(2,True,'/close ETHUSDT',now=now,positions={'margin_cross':{'ETHUSDT':{'id':'position','quantity':'0.04'}}})
        process_margin(self.q,receiver,book,self.shared,clock=clock)
        close_token=self.q.db.execute('SELECT id FROM unified_proposals').fetchone()[0]
        self.q.receive(3,True,'/mode spot',now=now)
        self.q.receive(4,True,'/confirm '+close_token,now=now)
        def crash(where):
            if where=='after_destination_commit':raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):process_margin(self.q,receiver,book,self.shared,clock=clock,fault=crash)
        self.assertFalse(book._positions())
        process_margin(self.q,receiver,book,self.shared,clock=clock)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE id='margin:close:telegram:4'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        self.q.receive(5,True,'/mode margin_cross',now=now);self.q.receive(6,True,'/report',now=now)
        process_margin(self.q,receiver,book,self.shared,clock=clock)
        report=self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id DESC').fetchone()[0]
        self.assertIn('Сегодня по Баку',report);self.assertIn('За всё время',report);self.assertIn('закрытий 1',report)

    def test_margin_repayment_confirmation_is_typed_and_idempotent(self):
        book=MarginPaperBook(self.shared.ledger);receiver=MarginCommandReceiver(book)
        now=time.time();ms=int(now*1000);clock=lambda:now
        f={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':ms}
        quotes={s:{'price':price,'time_ms':ms,'server_ms':ms,'source':'fixture','price_kind':'last_public_trade','filters':f} for s,price in [('BTCUSDT','100'),('ETHUSDT','200')]}
        book.record_quotes(quotes,now_ms=ms)
        proposal=self.shared.propose_transfer('margin-test','futures_isolated','margin_cross','50',now=now)
        self.shared.confirm_transfer(proposal['id'],now=now)
        receiver.protection_checked_ms=ms;receiver.apply(99,'/resume',{},clock_ms=lambda:ms)
        book.open('entry','position','BTCUSDT','LONG','.09','5',2,quotes,now_ms=ms)
        self.q.receive(1,True,'/mode margin_cross',now=now);self.q.receive(2,True,'/repay position:loan 1',now=now)
        process_margin(self.q,receiver,book,self.shared,clock=clock)
        token=self.shared.db.execute('SELECT id FROM margin_repay_proposals').fetchone()[0]
        self.q.receive(3,True,'/mode spot',now=now);self.q.receive(4,True,'/confirm '+token,now=now)
        def crash(where):
            if where=='after_destination_commit':raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):process_margin(self.q,receiver,book,self.shared,clock=clock,fault=crash)
        process_margin(self.q,receiver,book,self.shared,clock=clock)
        self.q.receive(5,True,'/confirm '+token,now=now);process_margin(self.q,receiver,book,self.shared,clock=clock)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_REPAY'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_margin_runtime_signal_to_interest_close_and_report_on_shared_copy(self):
        book=MarginPaperBook(self.shared.ledger);receiver=MarginCommandReceiver(book);strategy=MarginPaperStrategy(book)
        wall=[time.time()];clock=lambda:wall[0]
        class Reader:
            def snapshot(self,symbol,interval):
                now_ms=int(wall[0]*1000);period=900000;end=(now_ms//period)*period
                price='100' if symbol=='BTCUSDT' else '200'
                closes=[(end-(31-i)*period,'100') for i in range(30)]+[(end-period,'101' if symbol=='BTCUSDT' else '99')]
                filters={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':now_ms,'notional_price':price,'notional_time_ms':now_ms}
                return {'symbol':symbol,'price':price,'time_ms':now_ms,'server_ms':now_ms,'source':MarginSpotData.HOST,'price_kind':'last_public_trade','filters':filters,'candles':closes}
        reader=Reader()
        proposal=self.shared.propose_transfer('margin-cycle','futures_isolated','margin_cross','50',now=wall[0]);self.shared.confirm_transfer(proposal['id'],now=wall[0])
        self.q.receive(1,True,'/mode margin_cross',now=wall[0])
        cycle(self.shared,book,self.q,receiver,strategy,reader,started=0,clock=clock)
        receiver.protection_checked_ms=strategy.last_protection_ms
        self.q.receive(2,True,'/resume',now=wall[0]);wall[0]+=901
        # Refresh protection at the new candle while paused before resuming.
        cycle(self.shared,book,self.q,receiver,strategy,reader,started=0,clock=clock)
        receiver.protection_checked_ms=strategy.last_protection_ms
        self.q.receive(3,True,'/resume',now=wall[0]);process_margin(self.q,receiver,book,self.shared,clock=clock)
        wall[0]+=901
        actions=cycle(self.shared,book,self.q,receiver,strategy,reader,started=0,clock=clock)
        self.assertEqual([a['action'] for a in actions],['OPEN','OPEN'])
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM margin_loans WHERE status='OPEN'").fetchone()[0],2)
        self.q.receive(4,True,'/pause',now=wall[0]);wall[0]+=3601
        cycle(self.shared,book,self.q,receiver,strategy,reader,started=0,clock=clock)
        self.assertGreaterEqual(self.shared.db.execute('SELECT count(*) FROM margin_interest_periods').fetchone()[0],4)
        pos=self.shared.db.execute("SELECT id,quantity FROM margin_positions WHERE symbol='BTCUSDT'").fetchone()
        self.q.receive(5,True,'/close BTCUSDT',now=wall[0],positions={'margin_cross':{'BTCUSDT':dict(pos)}})
        process_margin(self.q,receiver,book,self.shared,clock=clock)
        token=self.q.db.execute('SELECT id FROM unified_proposals').fetchone()[0]
        self.q.receive(6,True,'/confirm '+token,now=wall[0]);process_margin(self.q,receiver,book,self.shared,clock=clock)
        self.q.receive(7,True,'/report',now=wall[0]);process_margin(self.q,receiver,book,self.shared,clock=clock)
        text=self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id DESC').fetchone()[0]
        self.assertIn('закрытий 1',text);self.assertIn('За всё время',text)
        self.assertEqual(self.shared.db.execute("SELECT status FROM margin_loans WHERE position_id=?",(pos['id'],)).fetchone()[0],'CLOSED')
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_margin_entrypoint_starts_paused_and_stays_paused_on_restart(self):
        if os.name!='posix':self.skipTest('Linux process locks required')
        path=Path(self.tmp.name);script='''import socket,sys,time
from unittest.mock import patch
import trading_bot.margin_app as app
class Reader:
 HOST='https://api.binance.com'
 def snapshot(self,symbol,interval):
  now=int(time.time()*1000);period=900000;end=(now//period)*period
  price='100' if symbol=='BTCUSDT' else '200'
  f={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':now,'notional_price':price,'notional_time_ms':now}
  return {'symbol':symbol,'price':price,'time_ms':now,'server_ms':now,'source':'https://api.binance.com','price_kind':'last_public_trade','filters':f,'candles':[(end-(31-i)*period,'100') for i in range(31)]}
def deny(*args,**kwargs):raise AssertionError('network prohibited')
with patch.object(socket,'socket',deny),patch.object(app,'MarginSpotData',Reader):
 sys.argv=['margin-app','--db',sys.argv[1],'--queue',sys.argv[2],'--once'];app.main()
'''
        for attempt in range(2):
            if attempt:
                with self.shared.transaction():self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
            result=subprocess.run([sys.executable,'-B','-c',script,str(path/'shared.db'),str(path/'queue.db')],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            health=json.loads((path/'margin-health.json').read_text())
            self.assertTrue(health['paused']);self.assertTrue(health['quotes_fresh']);self.assertTrue(health['protection_fresh']);self.assertIsNone(health['error'])
            self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)

    def test_margin_rollback_guard_refuses_new_journal_and_queue_events(self):
        root=Path(self.tmp.name);financial=root/'baseline-financial.db';queue=root/'baseline-queue.db'
        for source,target in ((self.shared.db,financial),(self.q.db,queue)):
            db=sqlite3.connect(target)
            try:source.backup(db)
            finally:db.close()
        guard=lambda:may_restore_pre_margin_code(financial,root/'shared.db',queue,root/'queue.db')
        self.assertTrue(guard()['safe_for_old_code'])
        self.q.receive(1,True,'/mode margin_cross')
        self.assertIn('queue_changed',guard()['reasons'])
        with self.shared.ledger.transaction():
            self.shared.ledger._post('fixture:new-financial-event','FIXTURE',{},[('reserve:free','USDT',Decimal(1)),('external:fixture','USDT',Decimal(-1))])
        self.assertIn('financial_history_changed',guard()['reasons'])

    def test_amount_display_preserves_sign_and_precision_only_for_rendering(self):
        cases={'0':'0','-0':'0','1.23456':'1.2346','-1.23456':'-1.2346',
               '0.0001':'0.0001','0.00009999999999999999999999999999999999':'меньше 0.0001',
               '-0.00000000001':'-меньше 0.0001','0.00000000001':'меньше 0.0001',
               '99999999999999999999.999999':'100000000000000000000'}
        for source,expected in cases.items():
            with self.subTest(source=source):self.assertEqual(_fmt(Decimal(source)),expected)
        self.assertEqual(_fmt(None),'неизвестно')
        with self.assertRaises(TypeError):_fmt(0.5)

    def test_all_wallet_report_commands_format_without_mutating_values(self):
        data=live_report(self.shared,now=time.time())
        data['wallets']['spot']['free']=Decimal('1.23456789')
        data['wallets']['spot']['reserved']=Decimal('0.00000001')
        data['btc']=Decimal('0.00000001');data['total']=Decimal('-0.00000001')
        data['cross']={'equity':Decimal('1.234567'),'margin':Decimal('0.00000001'),
                       'liability':Decimal('-0.00000001'),'buffer':Decimal('1.234567'),
                       'available':Decimal('1.234567'),'gross':Decimal('-0.00000001'),
                       'realized':Decimal(0),'fees':Decimal('0.00000001'),
                       'funding_total':Decimal('-0.00000001'),'adjustment':Decimal(0),
                       'positions':[{'symbol':'BTCUSDT','direction':'LONG','quantity':'0.00000001',
                                     'entry':'100.123456','margin':'4.123456'}]}
        data['wallets']['futures_cross']={'free':Decimal('1.23456789'),'reserved':Decimal('0.00000001')}
        self.q.receive(1,True,'/wallet spot');self.q.receive(2,True,'/wallet futures_cross')
        self.q.receive(3,True,'/wallets');self.q.receive(4,True,'/balance_report')
        self.q.receive(5,True,'/balance_report today');self.q.receive(6,True,'/balance_report all')
        with patch('trading_bot.paper_wallet_worker.live_report',return_value=data):self._process()
        messages=[r[0] for r in self.q.db.execute('select message from unified_outbox')]
        self.assertEqual(len(messages),6)
        for text in messages:
            self.assertNotIn('1.234567',text)
            self.assertIn('меньше 0.0001',text)
        self.assertIn('0.00000001 BTC',messages[0])
        self.assertIn('-меньше 0.0001',messages[3])
        for text in messages[3:]:self.assertIn('окончательность публикации источником не гарантирована',text)
        self.assertNotIn('Общий капитал',messages[2])
        self.assertEqual(messages[2].count('[Кошельки]'),1)
        self.assertEqual(data['wallets']['spot']['free'],Decimal('1.23456789'))
        self.assertEqual(data['cross']['positions'][0]['quantity'],'0.00000001')

    def test_mode_independent_transfer_and_retry_after_destination_commit(self):
        before=live_report(self.shared,now=time.time());future_pos=self.shared.snapshot('futures')['position']
        self.q.receive(1,True,'/mode futures');self.q.receive(2,True,'/transfer spot futures_isolated 50')
        self._process();proposal=self.shared.db.execute('select * from transfer_proposals').fetchone();token=proposal['id']
        self.assertTrue(token.startswith('T-'))
        self.q.receive(3,True,'/confirm '+token)
        def crash(p):
            if p=='after_destination_commit':raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):self._process(fault=crash)
        self.assertEqual(len(self.q.pending('wallet')),1)
        self._process();self.q.receive(4,True,'/confirm '+token);self._process()
        after=live_report(self.shared,now=time.time())
        self.assertEqual(before['total'],after['total'])
        self.assertEqual(self.shared.snapshot('futures')['position'],future_pos)
        self.assertEqual(self.shared.db.execute("select count(*) from ledger_events where kind='TRANSFER'").fetchone()[0],1)
        self.assertEqual(self.q.state('mode'),'futures')

    def test_wallet_reports_and_other_modes_not_available(self):
        self.q.receive(1,True,'/wallet spot');self.q.receive(2,True,'/wallet futures_cross');self.q.receive(3,True,'/balance_report today')
        self._process()
        results=[r[0] for r in self.q.db.execute('select message from unified_outbox')]
        self.assertTrue(any('свободно' in x and 'Spot' in x for x in results))
        self.assertTrue(any('Доступны reserve' in x for x in results))
        self.assertTrue(any('сегодня по Баку' in x for x in results))
        self.assertTrue(any('не подтверждены' in x for x in results))

    def test_capital_breakdown_matches_single_snapshot_without_mutation(self):
        before=list(self.shared.db.execute('SELECT id,fingerprint FROM ledger_events ORDER BY id'))
        data=live_report(self.shared,now=time.time())
        with localcontext() as ctx:
            ctx.prec=120
            self.assertEqual(sum(data['valuation_terms'].values(),Decimal(0)),data['total'])
        self.assertTrue(data['valuation_ok'])
        self.assertLess(data['valuation_terms']['isolated_entry_fee'],0)
        self.assertLess(data['valuation_terms']['isolated_exit_cost'],0)
        text=render_live_report(data)
        for label in ('Проверка расчёта оценки: пройдена','Проверка проводок и остатков: пройдена',
                      'оценочные издержки выхода','комиссия входа isolated','Futures isolated'):
            self.assertIn(label,text)
        self.assertEqual(before,list(self.shared.db.execute('SELECT id,fingerprint FROM ledger_events ORDER BY id')))
        self.assertNotIn('[Кошельки]',text)

    def test_transfer_availability_requires_pause_and_known_state(self):
        data=live_report(self.shared,now=time.time())
        self.assertEqual(data['available']['spot'],data['wallets']['spot']['free'])
        with self.shared.transaction():self.shared._set('spot','paused',False)
        data=live_report(self.shared,now=time.time())
        self.assertIsNone(data['available']['spot'])
        self.assertIsNotNone(data['available']['futures_isolated'])

    def test_informational_market_reply_is_durable_without_extra_ack_after_crash(self):
        store=RuntimeStore(Path(self.tmp.name)/'shared.db',{'mode':'paper','interval':'15m'},shared=True)
        self.addCleanup(store.close)
        before=store.db.execute('SELECT count(*) FROM runtime_outbox').fetchone()[0]
        self.q.receive(44,True,'/report')
        def crash(point):
            if point=='after_destination_commit':raise RuntimeError('injected failure')
        with self.assertRaises(RuntimeError):process_market(self.q,None,store,'spot',fault=crash)
        self.assertEqual(store.db.execute('SELECT count(*) FROM runtime_outbox').fetchone()[0],before+1)
        process_market(self.q,None,store,'spot')
        self.assertFalse(self.q.pending('spot'))
        self.assertEqual(store.db.execute('SELECT count(*) FROM runtime_outbox').fetchone()[0],before+1)
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM unified_outbox').fetchone()[0],0)

    def test_cancel_does_not_confirm_and_close_tokens_are_separate(self):
        self.q.receive(1,True,'/transfer spot reserve 10');self._process()
        token=self.shared.db.execute('select id from transfer_proposals').fetchone()[0]
        self.q.receive(2,True,'/cancel '+token);self._process()
        self.q.receive(3,True,'/confirm '+token);self._process()
        self.assertEqual(self.shared.ledger.balance('reserve:free'),0)
        self.assertEqual(self.shared.db.execute('select status from transfer_proposals where id=?',(token,)).fetchone()[0],'CANCELED')
        self.q.receive(4,True,'/confirm fake-close-token');self._process()
        self.assertEqual(self.shared.ledger.balance('reserve:free'),0)

    def test_report_marks_stale_prices_and_fees_as_unknown(self):
        report=live_report(self.shared,now=time.time()+1000)
        self.assertIsNone(report['total'])
        text=render_live_report(report)
        self.assertIn('устарел',text)
        self.assertIn('неизвестно',text)

    def test_telegram_ingress_transfer_confirmation_end_to_end(self):
        now=int(time.time());batches=[];sent=[]
        def msg(i,text,who=1):return {'update_id':i,'message':{'from':{'id':who},'chat':{'id':who,'type':'private'},'date':now,'text':text}}
        batches.append([msg(10,'/mode futures'),msg(11,'/transfer spot futures_isolated 25'),msg(12,'/transfer spot reserve 10',who=2)])
        def http(req):
            method=req.full_url.rsplit('/',1)[-1]
            if method=='getUpdates':return {'ok':True,'result':batches.pop(0)}
            sent.append(json.loads(req.data)['text']);return {'ok':True,'result':{}}
        t=TelegramTransport('FAKE','1',http,started=now-1)
        spot=RuntimeStore(Path(self.tmp.name)/'shared.db',{'mode':'paper','interval':'15m'},shared=True);self.addCleanup(spot.close)
        t.poll(spot,self.q);self._process();t.flush(self.q)
        self.assertEqual(self.shared.db.execute('select count(*) from transfer_proposals').fetchone()[0],1)
        token=self.shared.db.execute('select id from transfer_proposals').fetchone()[0]
        self.assertTrue(any(token in x for x in sent));batches.append([msg(13,'/confirm '+token)])
        t.poll(spot,self.q);self._process();t.flush(self.q)
        self.assertTrue(any('учтён один раз' in x for x in sent))
        self.assertEqual(self.shared.db.execute("select count(*) from ledger_events where kind='TRANSFER'").fetchone()[0],1)
        self.assertEqual(self.q.state('mode'),'futures')

    def test_observation_equity_excludes_internal_transfer_flow(self):
        obs=Path(self.tmp.name)/'observation.db'
        before=time.time();snapshot(Path(self.tmp.name)/'shared.db',obs,now=before)
        p=self.shared.propose_transfer('flow','spot','futures_isolated','20',now=before)
        self.shared.confirm_transfer(p['id'],now=before+1)
        snapshot(Path(self.tmp.name)/'shared.db',obs,now=before+2)
        with sqlite3.connect(obs) as db:values=[Decimal(r[0]) for r in db.execute('select equity from samples order by ts')]
        self.assertEqual(len(values),2);self.assertEqual(values[0],values[1])

    def test_today_funding_and_entry_fee_use_event_times_baku(self):
        path=Path(self.tmp.name)/'shared.db'
        f=FuturesStore(path,shared=True);self.addCleanup(f.close)
        pos=f.get('position');now=time.time();stamp=int(now*1000)-1000
        f.apply_funding(pos['id'],[{'time':stamp,'rate':'.002','mark':'100'}],int(now*1000))
        data=live_report(self.shared,now=now+1,period='today')
        with localcontext() as ctx:
            ctx.prec=28
            expected=Decimal(pos['quantity'])*Decimal('100')*Decimal('.002')
        self.assertEqual(data['funding_total'],expected)
        self.assertEqual(data['future_fees'],Decimal(pos['entry_fee']))
        self.assertIsNone(data['spot_fees'])
