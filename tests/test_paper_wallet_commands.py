import json
import sqlite3
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
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
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
                with sqlite3.connect(p/name) as db:src.backup(db)
        finally:spot.close();future.close()
        consolidate_snapshots(p/'spot-copy.db',p/'future-copy.db',p/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(p/'shared.db');self.q=UnifiedQueue(p/'queue.db')
        self.addCleanup(self.shared.close);self.addCleanup(self.q.close)
        now=time.time()
        with self.shared.transaction():
            self.shared._set('spot','quote',{'price':'100','time':now})
            self.shared._set('futures','quote',{'price':'100','time':now})

    def _process(self,**kwargs):process_wallet(self.q,self.shared,**kwargs)

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
            self.assertNotIn('1.234567',text);self.assertNotIn('0.00000001',text)
            self.assertIn('меньше 0.0001',text)
        self.assertIn('-меньше 0.0001',messages[2])
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
        self.assertTrue(any('свободно' in x and 'spot' in x for x in results))
        self.assertTrue(any('Доступны reserve' in x for x in results))
        self.assertTrue(any('сегодня по Баку' in x for x in results))
        self.assertTrue(any('не подтверждены' in x for x in results))

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
