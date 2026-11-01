import json
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.app import Application,TelegramTransport
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report,render_live_report
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
