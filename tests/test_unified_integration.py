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
from unittest.mock import patch

from trading_bot.app import Application, TelegramTransport
from trading_bot.futures_store import FuturesStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.testnet_observer import ObservationError
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.unified_receiver import MarketReceiver
from trading_bot.unified_worker import process_market


class UnifiedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.spot = RuntimeStore(root/'spot.db', {'mode':'paper','interval':'15m'})
        self.future = FuturesStore(root/'future.db')
        self.queue = UnifiedQueue(root/'queue.db')
        self.sr = MarketReceiver(root/'spot.db', 'spot')
        self.fr = MarketReceiver(root/'future.db', 'futures')
        for x in (self.spot, self.future, self.queue, self.sr, self.fr): self.addCleanup(x.close)
        self.app = Application(self.spot, PaperExchange(), None)

    def process(self, market, **kw):
        process_market(self.queue, self.sr if market == 'spot' else self.fr,
                       self.spot if market == 'spot' else self.future, market, **kw)

    def test_real_poller_routes_updates_and_unauthorized_is_ignored(self):
        now = int(time.time())
        def update(i, who, text):
            return {'update_id':i, 'message':{'from':{'id':who},'chat':{'id':who,'type':'private'},'date':now,'text':text}}
        updates = [update(100,1,'/pause'), update(101,1,'/mode futures'), update(102,2,'/resume'), update(103,1,'/pause')]
        calls=[]
        def http(req):
            calls.append(req.full_url.rsplit('/',1)[-1]);return {'ok':True,'result':updates}
        t=TelegramTransport('FAKE','1',http,started=now-1)
        t.poll(self.spot,self.queue)
        self.assertEqual(self.queue.state('mode'),'futures')
        self.assertEqual(self.queue.pending('spot')[0]['update_id'],100)
        self.assertEqual(self.queue.pending('futures')[0]['update_id'],103)
        self.process('spot'); self.process('futures')
        self.assertTrue(self.spot.get('paused')); self.assertTrue(self.future.get('paused'))
        self.assertEqual(calls,['getUpdates'])

    def test_crash_after_market_commit_before_queue_ack(self):
        self.queue.receive(3,True,'/pause')
        def fault(p):
            if p=='after_destination_commit':raise RuntimeError('injected crash')
        with self.assertRaises(RuntimeError): self.process('spot',fault=fault)
        self.assertTrue(self.spot.get('paused'))
        self.assertEqual(len(self.queue.pending('spot')),1)
        self.process('spot')
        self.assertFalse(self.queue.pending('spot'))
        self.assertEqual(self.spot.db.execute("select count(*) from runtime_meta where key='unified-applied:3'").fetchone()[0],1)

    def test_partial_pause_all_other_market_continues(self):
        self.queue.receive(1,True,'/pause_all')
        self.process('spot')
        self.assertEqual(len(self.queue.pending('futures')),1)
        self.queue.receive(2,True,'/signal')
        self.process('spot')
        self.assertFalse(self.queue.pending('spot'))
        self.assertTrue(any('ожидается futures' in r[0] for r in self.queue.db.execute('select message from unified_outbox')))
        self.process('futures')
        self.assertTrue(any('применены к обоим' in r[0] for r in self.queue.db.execute('select message from unified_outbox')))

    def test_restarted_worker_rejects_queued_resume(self):
        self.queue.receive(1,True,'/resume',now=10)
        self.process('spot',started=11)
        self.assertTrue(self.spot.get('paused'))
        self.assertFalse(self.queue.pending('spot'))

    def test_confirm_close_rechecks_at_prepare_and_claim(self):
        self.spot.command(1,True,'/resume')
        self.app.execute('BUY','.1',Decimal(100),'entry','SIGNAL')
        pos=self.spot.get('position')
        self.queue.receive(10,True,'/close',positions={'spot':pos})
        self.process('spot')
        token=self.queue.db.execute('select id from unified_proposals').fetchone()[0]
        self.queue.receive(11,True,'/confirm '+token)
        self.process('spot')
        req=self.spot.get('close_request')
        self.assertEqual(req['target'],pos['id'])
        with self.spot.transaction(): self.spot.set('position',{**pos,'quantity':'.05'})
        with self.assertRaises(ObservationError): self.spot.prepare('SELL','.05',Decimal(100),'stale','MANUAL',target=pos['id'])
        with self.spot.transaction(): self.spot.set('position',pos)
        cid=self.spot.prepare('SELL','.1',Decimal(100),'prepared','MANUAL',target=pos['id'])
        with patch('trading_bot.runtime_store.time.time',return_value=req['expires']+1):
            self.assertIsNone(self.spot.claim(cid))
        row=self.spot.db.execute('select phase,status from runtime_orders where client_id=?',(cid,)).fetchone()
        self.assertEqual(tuple(row),('SETTLED','EXPIRED'))
        auto=self.spot.prepare('SELL','.1',Decimal(100),'protective','STOP',target=pos['id'])
        self.assertIsNotNone(self.spot.claim(auto))

    def test_close_queued_before_position_change_does_not_target_new_position(self):
        self.spot.command(1,True,'/resume')
        self.app.execute('BUY','.1',Decimal(100),'entry','SIGNAL')
        old=self.spot.get('position')
        self.queue.receive(10,True,'/close',positions={'spot':old})
        with self.spot.transaction(): self.spot.set('position',{**old,'id':'replacement'})
        self.process('spot')
        self.assertEqual(self.queue.db.execute('select count(*) from unified_proposals').fetchone()[0],0)

    def test_futures_manual_close_expiry_preserves_protective_exit(self):
        self.future.command(1,True,'/resume')
        self.future.open_position('SHORT',Decimal(100),1000,'entry')
        pos=self.future.get('position');self.future.apply_funding(pos['id'],[],2000)
        with self.future.transaction():
            self.future.set('close_request',{'target':pos['id'],'quantity':pos['quantity'],'expires':10,'update_id':5})
        with patch('trading_bot.futures_store.time.time',return_value=11):
            self.assertFalse(self.future.close_position(Decimal(100),2000,'manual','MANUAL',pos['id']))
            self.assertTrue(self.future.close_position(Decimal(100),2000,'protection','STOP',pos['id']))

    def test_futures_manual_close_rejects_changed_volume_without_trade(self):
        self.future.command(1,True,'/resume')
        self.future.open_position('SHORT',Decimal(100),1000,'entry')
        pos=self.future.get('position');self.future.apply_funding(pos['id'],[],2000)
        with self.future.transaction():
            self.future.set('close_request',{'target':pos['id'],'quantity':str(Decimal(pos['quantity'])/2),'expires':time.time()+120,'update_id':5})
        self.assertFalse(self.future.close_position(Decimal(100),2000,'manual','MANUAL',pos['id']))
        self.assertEqual(self.future.get('position')['id'],pos['id'])
        self.assertEqual(self.future.db.execute("select count(*) from futures_events where event='CLOSE'").fetchone()[0],0)

    def test_two_receivers_apply_same_update_once(self):
        barrier=threading.Barrier(2); results=[]; errors=[]
        def run():
            try:
                r=MarketReceiver(Path(self.tmp.name)/'spot.db','spot')
                try: barrier.wait(); results.append(r.apply(20,'/pause'))
                finally:r.close()
            except Exception as e:errors.append(e)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[]);self.assertEqual(len(results),2)
        self.assertEqual(self.spot.db.execute("select count(*) from runtime_meta where key='unified-applied:20'").fetchone()[0],1)

    def test_real_sqlite_lock_never_advances_source_offset(self):
        self.queue.db.execute('PRAGMA busy_timeout=20')
        blocker=sqlite3.connect(Path(self.tmp.name)/'queue.db',isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute('BEGIN IMMEDIATE')
        now=int(time.time())
        updates=[{'update_id':100,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'date':now,'text':'/pause'}},
                 {'update_id':105,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'date':now,'text':'/mode futures'}}]
        t=TelegramTransport('FAKE','1',lambda req:{'ok':True,'result':updates},started=now-1)
        with self.assertRaises(sqlite3.OperationalError):t.poll(self.spot,self.queue)
        self.assertEqual(self.queue.state('offset'),'0')
        blocker.rollback();t.poll(self.spot,self.queue)
        self.assertEqual(self.queue.state('offset'),'106')
        self.assertEqual(self.queue.pending('spot')[0]['update_id'],100)
        self.assertEqual(self.queue.state('mode'),'futures')

    def test_process_death_after_destination_commit_then_restart(self):
        self.queue.receive(12,True,'/pause')
        code=("import os,sys; from trading_bot.unified_receiver import MarketReceiver; "
              "r=MarketReceiver(sys.argv[1],'spot',fault=lambda p: os._exit(73) if p=='after_commit' else None); "
              "r.apply(12,'/pause')")
        p=subprocess.run([sys.executable,'-c',code,str(Path(self.tmp.name)/'spot.db')])
        self.assertEqual(p.returncode,73)
        self.assertTrue(self.spot.get('paused'))
        self.process('spot')
        self.assertFalse(self.queue.pending('spot'))
        self.assertEqual(self.spot.db.execute("select count(*) from runtime_meta where key='unified-applied:12'").fetchone()[0],1)
