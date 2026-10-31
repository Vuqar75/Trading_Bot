import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.futures_store import FuturesStore
from trading_bot.futures_app import FuturesApplication
from trading_bot.futures_data import FuturesData
from trading_bot.app import TelegramTransport
from trading_bot.testnet_observer import ObservationError


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'futures.db'
        self.s=FuturesStore(self.path,slippage_bps='0')
        self.addCleanup(lambda:self.s.close())
        self.s.command(1,True,'/leverage 2')
        self.s.command(2,True,'/resume')

    def open(self,direction='LONG'):
        self.assertTrue(self.s.open_position(direction,Decimal(100),1000,'open'))
        return self.s.get('position')

    def close(self,price=102):
        p=self.s.get('position')
        self.s.apply_funding(p['id'],[],3000)
        return self.s.close_position(Decimal(price),3000,'close','TEST',p['id'])

    def test_long_net_and_margin(self):
        p=self.open()
        self.assertEqual(Decimal(p['quantity']),Decimal('.2'))
        self.assertEqual(Decimal(self.s.get('cash')),990)
        self.close()
        self.assertEqual(Decimal(self.s.get('pnl')),Decimal('.3798'))
        self.assertEqual(Decimal(self.s.get('cash')),Decimal('1000.3798'))
        self.assertIsNone(self.s.get('position'))

    def test_short_profit(self):
        self.open('SHORT')
        self.close(98)
        self.assertEqual(Decimal(self.s.get('pnl')),Decimal('.3802'))

    def test_all_leverages_and_gates(self):
        self.s.command(3,True,'/pause')
        for lev in range(1,11):
            self.s.command(lev+3,True,f'/leverage {lev}')
            self.assertEqual(self.s.get('leverage'),lev)
        self.s.command(20,True,'/leverage 11')
        self.assertEqual(self.s.get('leverage'),10)
        self.s.command(21,True,'/resume')
        self.open()
        self.s.command(22,True,'/pause')
        self.s.command(23,True,'/leverage 1')
        self.assertEqual(self.s.get('leverage'),10)

    def test_funding_signs_replay_and_late_publication(self):
        p=self.open()
        event={'time':2000,'rate':'.001','mark':'100'}
        self.s.apply_funding(p['id'],[],3000)
        self.s.apply_funding(p['id'],[event],3000)
        self.s.apply_funding(p['id'],[event],3000)
        self.assertEqual(Decimal(self.s.get('position')['funding']),Decimal('-.02'))
        self.close(100)
        self.assertEqual(Decimal(self.s.get('pnl')),Decimal('-.04'))

    def test_short_receives_positive_funding(self):
        p=self.open('SHORT')
        self.s.apply_funding(p['id'],[{'time':2000,'rate':'.001','mark':'100'}],3000)
        self.assertEqual(Decimal(self.s.get('position')['funding']),Decimal('.02'))

    def test_changed_funding_rollback(self):
        p=self.open()
        self.s.apply_funding(p['id'],[{'time':2000,'rate':'.001','mark':'100'}],3000)
        with self.assertRaises(ObservationError):
            self.s.apply_funding(p['id'],[{'time':2000,'rate':'.002','mark':'100'}],3000)
        self.assertEqual(Decimal(self.s.get('position')['funding']),Decimal('-.02'))

    def test_loss_capped_at_isolated_margin(self):
        self.open('SHORT')
        self.close(1000)
        self.assertEqual(Decimal(self.s.get('cash')),990)
        self.assertEqual(Decimal(self.s.get('pnl')),-10)
        p=json.loads(self.s.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE'").fetchone()[0])
        self.assertGreater(Decimal(p['cap_adjustment']),0)

    def test_idempotent_entry_and_close(self):
        p=self.open()
        self.assertFalse(self.s.open_position('LONG',Decimal(100),1000,'open'))
        self.close()
        self.assertFalse(self.s.close_position(Decimal(102),3000,'close','TEST',p['id']))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM futures_events').fetchone()[0],2)

    def test_daily_loss_prevents_entry(self):
        self.open('SHORT')
        self.close(1000)
        self.assertFalse(self.s.open_position('LONG',Decimal(100),4000,'next'))

    def test_restart_pauses_retains_position_and_leverage(self):
        p=self.open()
        self.s.close()
        self.s=FuturesStore(self.path,slippage_bps='0')
        FuturesApplication(self.s,None)
        self.assertTrue(self.s.get('paused'))
        self.assertEqual(self.s.get('position'),p)
        self.assertEqual(self.s.get('leverage'),2)

    def test_identity_and_foreign_schema_refused(self):
        with self.assertRaises(ObservationError): FuturesStore(self.path,fee_bps='6',slippage_bps='0')
        foreign=Path(self.tmp.name)/'spot.db'
        with sqlite3.connect(foreign) as db: db.execute('CREATE TABLE runtime_meta(x)')
        with self.assertRaises(ObservationError): FuturesStore(foreign)

    def test_funding_required_before_close(self):
        p=self.open()
        with self.assertRaises(ObservationError): self.s.close_position(Decimal(102),3000,'close','TEST',p['id'])
        self.assertIsNotNone(self.s.get('position'))

    def test_liquidation_and_stop_work_while_paused_without_candles(self):
        self.open('SHORT')
        self.s.command(3,True,'/pause')
        class Reader:
            def mark(self): return Decimal(200),3000
            def funding(self,*args):return []
            def candles(self,*args):raise AssertionError('Risk must run before candles')
        app=FuturesApplication(self.s,Reader())
        app.tick()
        self.assertIsNone(self.s.get('position'))
        payload=json.loads(self.s.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE'").fetchone()[0])
        self.assertEqual(payload['reason'],'MODEL_LIQUIDATION')

    def test_reversal_waits_for_next_candle(self):
        self.open('LONG')
        class Reader:
            stamp=2000
            def mark(self): return Decimal(100),3000
            def funding(self,*args): return []
            def candles(self,*args): return [(self.stamp+i,Decimal(100)) for i in range(31)]
        r=Reader()
        app=FuturesApplication(self.s,r)
        self.s.command(3,True,'/resume')
        details={'action':'SELL','fast':99,'slow':100,'old_fast':101,'old_slow':100,'count':31}
        with patch('trading_bot.futures_app.signal_details',return_value=details):
            app.tick()
            self.assertIsNone(self.s.get('position'))
            app.tick()
            self.assertIsNone(self.s.get('position'))
            r.stamp+=1000
            app.tick()
            self.assertEqual(self.s.get('position')['direction'],'SHORT')

    def test_timeframe_persistence_and_command_replay(self):
        self.s.command(3,True,'/pause')
        self.s.command(4,True,'/timeframe 5m')
        self.s.command(4,True,'/timeframe 5m')
        self.s.close()
        self.s=FuturesStore(self.path,slippage_bps='0')
        self.assertEqual(self.s.get('interval'),'5m')
        self.assertTrue(self.s.get('paused'))

    def test_status_and_history_are_readable(self):
        self.open()
        self.close()
        self.s.command(3,True,'/status')
        self.s.command(4,True,'/history')
        text='\n'.join(r[0] for r in self.s.db.execute('SELECT message FROM runtime_outbox'))
        for word in ['Isolated','LONG','funding','Баку','Net']:
            self.assertIn(word,text)

    def test_wallet_operations_atomic_on_crash(self):
        code='''
import os,sys
from decimal import Decimal
from trading_bot.futures_store import FuturesStore
s=FuturesStore(sys.argv[1],slippage_bps='0')
s.fault=lambda p:os._exit(73) if p==sys.argv[2] else None
s.open_position('LONG',Decimal(100),1000,'open')
'''
        for point in ['before_commit','after_commit']:
            path=Path(self.tmp.name)/(point+'.db')
            s=FuturesStore(path,slippage_bps='0')
            s.command(1,True,'/resume')
            s.close()
            result=subprocess.run([sys.executable,'-B','-c',code,str(path),point],capture_output=True)
            self.assertEqual(result.returncode,73)
            s=FuturesStore(path,slippage_bps='0')
            try:
                self.assertEqual(bool(s.get('position')),point=='after_commit')
                self.assertEqual(Decimal(s.get('cash')),990 if point=='after_commit' else 1000)
            finally:s.close()


class FuturesDataTests(unittest.TestCase):
    def test_only_get_public_allowlist(self):
        requests=[]
        reader=FuturesData(lambda r:requests.append(r) or {'serverTime':1})
        reader.get('/fapi/v1/time')
        self.assertTrue(requests[0].full_url.startswith('https://fapi.binance.com/'))
        self.assertEqual(requests[0].get_method(),'GET')
        with self.assertRaises(ObservationError):reader.get('/fapi/v1/order')

    def test_mark_staleness(self):
        reader=FuturesData(lambda r:{'serverTime':100000} if r.full_url.endswith('/time') else {'symbol':'BTCUSDT','time':1,'markPrice':'100'})
        with self.assertRaises(ObservationError):reader.mark()

    def test_funding_pages_and_negative_rate(self):
        calls=[]
        def transport(req):
            calls.append(req)
            return [{'symbol':'BTCUSDT','fundingTime':2000,'fundingRate':'-.001','markPrice':'100'}]
        reader=FuturesData(transport)
        rows=reader.funding(1001,3000)
        self.assertEqual(rows,[{'time':2000,'rate':'-0.001','mark':'100'}])
        self.assertIn('endTime=3000',calls[0].full_url)

    def test_funding_wrong_order_fails(self):
        row={'symbol':'BTCUSDT','fundingTime':2000,'fundingRate':'.001','markPrice':'100'}
        reader=FuturesData(lambda r:[row,row])
        with self.assertRaises(ObservationError):reader.funding(1001,3000)


class FuturesIntegrationTests(unittest.TestCase):
    def test_telegram_leverage_and_flat_close(self):
        import time
        now=int(time.time())
        updates=[{'update_id':1,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'date':now,'text':'/leverage 7'}},
                 {'update_id':2,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'date':now,'text':'/close'}}]
        with tempfile.TemporaryDirectory() as directory:
            s=FuturesStore(Path(directory)/'futures.db')
            try:
                t=TelegramTransport('FAKE','1',lambda req:{'ok':True,'result':updates},started=now)
                t.poll(s)
                self.assertEqual(s.get('leverage'),7)
                self.assertIsNone(s.get('close_request'))
                self.assertEqual(s.get('offset'),3)
            finally:s.close()

    def test_equity_and_backup(self):
        from trading_bot.runtime_backup import backup
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'futures.db'
            dest=Path(directory)/'snapshot.db'
            s=FuturesStore(source,slippage_bps='0')
            try:
                s.command(1,True,'/resume')
                s.open_position('LONG',Decimal(100),1000,'open')
                s.record_equity(Decimal(99),2000)
                self.assertLess(Decimal(s.get('equity')),1000)
                self.assertGreater(Decimal(s.get('max_drawdown')),0)
                backup(source,dest,schema='futures')
                restored=FuturesStore(dest,slippage_bps='0')
                try:self.assertEqual(restored.get('position'),s.get('position'))
                finally:restored.close()
            finally:s.close()
