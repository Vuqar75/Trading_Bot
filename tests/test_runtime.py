import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.app import Application, TelegramTransport, process_lock
from trading_bot.runtime_exchange import PaperExchange, TestnetExchange, order_quantity, market_snapshot
from trading_bot.runtime_store import RuntimeStore
from trading_bot.testnet_observer import ObservationError


def symbol_info():
    return {'symbol': 'BTCUSDT', 'baseAsset': 'BTC', 'quoteAsset': 'USDT', 'status': 'TRADING',
            'orderTypes': ['MARKET'], 'filters': [
                {'filterType': 'LOT_SIZE', 'stepSize': '0.00001', 'minQty': '0.00001', 'maxQty': '100'},
                {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '0', 'minQty': '0', 'maxQty': '10'},
                {'filterType': 'MIN_NOTIONAL', 'minNotional': '5'}]}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'runtime.db'
        self.identity = {'mode': 'paper'}
        self.store = RuntimeStore(self.path, self.identity)
        self.addCleanup(lambda: self.store.close())
        self.app = Application(self.store, PaperExchange(), None)
        self.store.command(1, True, '/resume')

    def buy(self):
        self.app.execute('BUY', '0.1', Decimal('100'), 'entry', 'TEST')
        return self.store.get('position')

    def test_roundtrip_exact_fees(self):
        pos = self.buy()
        self.app.execute('SELL', pos['quantity'], Decimal('102'), 'exit', 'TEST', pos['id'])
        self.assertEqual(Decimal(self.store.get('pnl')), Decimal('0.1798'))
        self.assertEqual(Decimal(self.store.get('cash')), Decimal('1000.1798'))
        self.assertFalse(self.store.pending())

    def test_restart_pauses_and_recovers_unknown_paper(self):
        cid = self.store.prepare('BUY', '0.1', Decimal(100), 'entry', 'TEST')
        self.store.claim(cid)
        self.store.close()
        self.store = RuntimeStore(self.path, self.identity)
        self.app = Application(self.store, PaperExchange(), None)
        self.assertTrue(self.store.get('paused'))
        self.app.recover()
        self.assertEqual(Decimal(self.store.get('position')['quantity']), Decimal('.1'))
        self.assertEqual(len(self.store.db.execute('SELECT * FROM runtime_fills').fetchall()), 1)

    def test_repeat_fill_and_terminal_do_not_duplicate(self):
        self.buy()
        order = self.store.db.execute('SELECT * FROM runtime_orders').fetchone()
        report = PaperExchange.inspect_saved(order)
        before = self.store.get('cash')
        self.store.apply(order['client_id'], report)
        self.store.apply(order['client_id'], report)
        self.assertEqual(self.store.get('cash'), before)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM runtime_fills').fetchone()[0], 1)

    def test_partial_then_full(self):
        cid = self.store.prepare('BUY', '0.1', Decimal(100), 'entry', 'TEST')
        self.store.claim(cid)
        row = self.store.db.execute('SELECT * FROM runtime_orders').fetchone()
        full = PaperExchange.inspect_saved(row)
        part = copy.deepcopy(full)
        part['order'].update(status='PARTIALLY_FILLED', executedQty='0.04', cummulativeQuoteQty='4')
        part['fills'][0].update(qty='0.04', quoteQty='4', commission='0.004')
        full['fills'] = [part['fills'][0], {**full['fills'][0], 'id': 'second', 'qty': '0.06', 'quoteQty': '6', 'commission': '0.006'}]
        self.store.apply(cid, part)
        with self.assertRaises(ObservationError): self.store.prepare('BUY', '.1', Decimal(100), 'next', 'TEST')
        self.store.apply(cid, full)
        self.assertEqual(Decimal(self.store.get('position')['quantity']), Decimal('.1'))
        self.assertEqual(Decimal(self.store.get('cash')), Decimal('989.990'))

    def test_unknown_submit_never_resends(self):
        class TimeoutExchange(PaperExchange):
            calls = 0
            def submit(self, *args):
                self.calls += 1
                raise TimeoutError()
        exchange = TimeoutExchange()
        app = Application(self.store, exchange, None)
        self.store.command(2, True, '/resume')
        app.execute('BUY', '0.1', Decimal(100), 'entry', 'TEST')
        self.assertEqual(self.store.pending()[0]['phase'], 'UNKNOWN')
        app.recover()
        app.recover()
        self.assertEqual(exchange.calls, 1)
        self.assertFalse(self.store.pending())

    def test_claim_rechecks_position_and_pause(self):
        cid = self.store.prepare('BUY', '0.1', Decimal(100), 'entry', 'TEST')
        self.store.command(2, True, '/pause')
        with self.assertRaises(ObservationError): self.store.claim(cid)
        self.assertEqual(self.store.pending()[0]['phase'], 'PREPARED')

    def test_stale_close_cannot_claim(self):
        pos = self.buy()
        cid = self.store.prepare('SELL', '.1', Decimal(100), 'exit', 'TEST', target=pos['id'])
        with self.store.transaction(): self.store.set('position', {**pos, 'id': 'new-position'})
        with self.assertRaises(ObservationError): self.store.claim(cid)

    def test_position_id_changes_on_next_entry(self):
        pos = self.buy()
        self.app.execute('SELL', '.1', Decimal(100), 'exit', 'TEST', pos['id'])
        self.app.execute('BUY', '.1', Decimal(100), 'entry2', 'TEST')
        self.assertNotEqual(pos['id'], self.store.get('position')['id'])

    def test_unknown_fee_stays_unknown_and_prevents_resume(self):
        cid = self.store.prepare('BUY', '.1', Decimal(100), 'entry', 'TEST')
        self.store.claim(cid)
        report = PaperExchange.inspect_saved(self.store.pending()[0])
        report['fills'][0].update(commissionAsset='BNB', commission='0.00001')
        self.store.apply(cid, report)
        self.assertFalse(self.store.get('position')['fees_known'])
        self.store.command(2, True, '/pause')
        self.store.command(3, True, '/resume')
        self.assertTrue(self.store.get('paused'))

    def test_conflicting_fills_rollback(self):
        self.buy()
        row = self.store.db.execute('SELECT * FROM runtime_orders').fetchone()
        report = PaperExchange.inspect_saved(row)
        report['fills'][0]['commission'] = '0.9'
        before = self.store.get('cash')
        with self.assertRaises(ObservationError): self.store.apply(row['client_id'], report)
        self.assertEqual(before, self.store.get('cash'))

    def test_partial_status_full_fills_rejected(self):
        cid = self.store.prepare('BUY', '.1', Decimal(100), 'entry', 'TEST')
        self.store.claim(cid)
        report = PaperExchange.inspect_saved(self.store.pending()[0])
        report['order']['status'] = 'PARTIALLY_FILLED'
        with self.assertRaises(ObservationError): self.store.apply(cid, report)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM runtime_fills').fetchone()[0], 0)

    def test_foreign_db_and_wrong_identity_refused(self):
        with self.assertRaises(ObservationError): RuntimeStore(self.path, {'mode': 'testnet'})
        legacy = Path(self.tmp.name) / 'legacy.db'
        with sqlite3.connect(legacy) as conn: conn.execute('CREATE TABLE state(x)')
        with self.assertRaises(ObservationError): RuntimeStore(legacy, self.identity)

    def test_commands_atomic_and_unauthorized_ignored(self):
        self.store.command(2, False, '/pause')
        self.assertFalse(self.store.get('paused'))
        self.store.command(3, True, '/pause')
        count = self.store.db.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0]
        self.store.command(3, True, '/pause')
        self.assertEqual(count, self.store.db.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0])
        self.assertEqual(self.store.get('offset'), 4)

    def test_command_commit_failure_rolls_back_offset_and_action(self):
        self.store.fault = lambda point: (_ for _ in ()).throw(RuntimeError()) if point == 'before_commit' else None
        with self.assertRaises(RuntimeError): self.store.command(2, True, '/pause')
        self.assertFalse(self.store.get('paused'))
        self.assertEqual(self.store.get('offset'), 2)
        self.store.fault = lambda _: None

    def test_lock_excludes_second_process(self):
        with process_lock(str(self.path) + '.lock'):
            with self.assertRaises(ObservationError):
                with process_lock(str(self.path) + '.lock'): pass

    def test_process_crash_before_and_after_commit(self):
        code = '''
import os, sys
from trading_bot.runtime_store import RuntimeStore
s=RuntimeStore(sys.argv[1], {'mode':'paper'})
s.fault=lambda p: os._exit(73) if p==sys.argv[2] else None
s.command(2,True,'/pause')
'''
        for point in ('before_commit', 'after_commit'):
            path = Path(self.tmp.name) / (point + '.db')
            s = RuntimeStore(path, self.identity)
            s.command(1, True, '/resume')
            s.close()
            result = subprocess.run([sys.executable, '-B', '-c', code, str(path), point], capture_output=True)
            self.assertEqual(result.returncode, 73)
            s = RuntimeStore(path, self.identity)
            try:
                self.assertEqual(s.get('paused'), point == 'after_commit')
                self.assertEqual(s.get('offset'), 3 if point == 'after_commit' else 2)
            finally: s.close()

    def test_real_telegram_auth_and_stale_close(self):
        now = int(time.time())
        updates = [
            {'update_id': 2, 'message': {'from': {'id': 99}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/pause', 'date': now}},
            {'update_id': 3, 'message': {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'group'}, 'text': '/pause', 'date': now}},
            {'update_id': 4, 'message': {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/close', 'date': now - 500}},
        ]
        telegram = TelegramTransport('FAKE', '1', lambda req: {'ok': True, 'result': updates}, started=now)
        telegram.poll(self.store)
        self.assertFalse(self.store.get('paused'))
        self.assertIsNone(self.store.get('close_request'))
        self.assertEqual(self.store.get('offset'), 5)

    def test_reply_failure_does_not_repeat_action(self):
        self.store.command(2, True, '/pause')
        count = self.store.db.execute('SELECT COUNT(*) FROM runtime_commands').fetchone()[0]
        telegram = TelegramTransport('FAKE', '1', lambda req: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(TimeoutError): telegram.flush(self.store)
        self.assertEqual(count, self.store.db.execute('SELECT COUNT(*) FROM runtime_commands').fetchone()[0])
        self.assertTrue(self.store.db.execute('SELECT 1 FROM runtime_outbox').fetchone())


    def test_protective_close_does_not_require_candles(self):
        self.buy()
        class Reader:
            def get(self, path, params=None):
                if path.endswith('price'): return {'symbol': 'BTCUSDT', 'price': '97'}
                if path.endswith('exchangeInfo'): return {'symbols': [symbol_info()]}
                raise AssertionError('Protection must not fetch candles')
        self.app.reader = Reader()
        self.store.command(2, True, '/pause')
        self.app.tick()
        self.assertEqual(Decimal(self.store.get('position')['quantity']), 0)
        self.assertTrue(self.store.get('paused'))

    def test_backup_and_restore(self):
        from trading_bot.runtime_backup import backup
        self.buy()
        destination = Path(self.tmp.name) / 'backup.db'
        backup(self.path, destination)
        restored = RuntimeStore(destination, self.identity)
        try:
            self.assertEqual(restored.get('position'), self.store.get('position'))
            self.assertEqual(restored.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally: restored.close()

    def test_full_tick_signal_enters_only_after_resume(self):
        now = 40 * 900000
        class Reader:
            def get(self, path, params=None):
                if path.endswith('price'): return {'symbol': 'BTCUSDT', 'price': '100'}
                if path.endswith('time'): return {'serverTime': now}
                if path.endswith('exchangeInfo'): return {'symbols': [symbol_info()]}
                return [[i*900000,0,0,0,'110' if i==39 else '100',0,(i+1)*900000-1] for i in range(40)]
        self.app.reader = Reader()
        self.app.tick()
        self.app.tick()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM runtime_orders').fetchone()[0], 1)
        self.assertEqual(Decimal(self.store.get('position')['quantity']), Decimal('.1'))

    def test_concurrent_preparation_reserves_single_order(self):
        other = RuntimeStore(self.path, self.identity)
        try:
            self.store.prepare('BUY', '.1', Decimal(100), 'first', 'TEST')
            with self.assertRaises(ObservationError): other.prepare('BUY', '.1', Decimal(100), 'second', 'TEST')
        finally: other.close()

    def test_entry_budget_cap(self):
        with self.assertRaises(ObservationError): self.app.execute('BUY', '1', Decimal(100), 'too-large', 'TEST')
        self.assertFalse(self.store.pending())


    def test_process_crash_during_fill_commit(self):
        code = """
import os,sys
from trading_bot.runtime_store import RuntimeStore
from trading_bot.runtime_exchange import PaperExchange
s=RuntimeStore(sys.argv[1], {'mode':'paper'})
r=s.pending()[0]
report=PaperExchange.inspect_saved(r)
s.fault=lambda p: os._exit(73) if p==sys.argv[2] else None
s.apply(r['client_id'],report)
"""
        for point in ('before_commit', 'after_commit'):
            path = Path(self.tmp.name)/(point+'-fill.db')
            store = RuntimeStore(path,self.identity)
            store.command(1,True,'/resume')
            cid = store.prepare('BUY','.1',Decimal(100),'entry','TEST')
            store.claim(cid)
            store.close()
            result = subprocess.run([sys.executable,'-B','-c',code,str(path),point],capture_output=True)
            self.assertEqual(result.returncode,73)
            store = RuntimeStore(path,self.identity)
            try:
                app = Application(store,PaperExchange(),None)
                app.recover()
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM runtime_fills').fetchone()[0],1)
                self.assertEqual(Decimal(store.get('cash')),Decimal('989.99'))
            finally: store.close()


    def test_confirmed_rejection_does_not_auto_retry(self):
        cid = self.store.prepare('BUY','.1',Decimal(100),'entry','TEST')
        self.store.claim(cid)
        report = PaperExchange.inspect_saved(self.store.pending()[0])
        report['order'].update(status='REJECTED',executedQty='0',cummulativeQuoteQty='0')
        report['fills'] = []
        self.store.apply(cid,report)
        self.app.recover()
        self.assertEqual(self.store.get('blocked'),'ORDER_REJECTED_REVIEW')
        self.store.command(2,True,'/resume')
        self.assertTrue(self.store.get('paused'))
        with self.assertRaises(ObservationError):
            self.app.execute('BUY','.1',Decimal(100),'next','TEST')


class ExchangeTests(unittest.TestCase):
    def test_filters_and_dust(self):
        self.assertEqual(order_quantity(symbol_info(), Decimal('.000149'), Decimal(70000)), '0.00014')
        with self.assertRaises(ObservationError): order_quantity(symbol_info(), Decimal('.00001'), Decimal(70000))
        bad = symbol_info()
        bad['filters'].append({'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.001', 'maxQty': '1', 'stepSize': '0.001'})
        with self.assertRaises(ObservationError): order_quantity(bad, Decimal('.00014'), Decimal(70000))

    def test_testnet_submit_one_post_and_explicit_enable(self):
        calls = []
        def transport(req):
            calls.append(req)
            if req.get_method() == 'GET': return {'serverTime': 1}
            raise TimeoutError()
        exchange = TestnetExchange('FAKE', 'FAKE', transport=transport)
        with self.assertRaises(ObservationError): exchange.submit('rt1', 'BUY', '.1')
        self.assertFalse(calls)
        exchange.enabled = True
        with self.assertRaises(TimeoutError): exchange.submit('rt1', 'BUY', '.1')
        self.assertEqual([r.get_method() for r in calls], ['GET', 'POST'])
        self.assertTrue(all(r.full_url.startswith('https://testnet.binance.vision/') for r in calls))

    def test_market_snapshot_gap_and_freshness(self):
        now = 900 * 1000 * 40
        rows = [[n * 900000, 0, 0, 0, '100', 0, (n+1)*900000-1] for n in range(40)]
        class Reader:
            def get(self, path, params=None):
                return {'serverTime': now} if path.endswith('time') else {'symbol': 'BTCUSDT', 'price': '100'} if path.endswith('price') else rows
        self.assertEqual(len(market_snapshot(Reader(), '15m')[1]), 40)
        rows.pop(20)
        with self.assertRaises(ObservationError): market_snapshot(Reader(), '15m')



class TestnetEndToEndTests(unittest.TestCase):
    def test_signed_adapter_to_runtime_ledger_and_restart(self):
        from urllib.parse import parse_qs, urlparse
        orders, fills, posts = {}, {}, []
        def transport(req):
            path = urlparse(req.full_url).path
            params = parse_qs(req.data.decode() if req.data else urlparse(req.full_url).query)
            if path.endswith('/time'): return {'serverTime': int(time.time()*1000)}
            if req.get_method() == 'POST':
                cid, side, quantity = (params[k][0] for k in ('newClientOrderId','side','quantity'))
                posts.append(cid)
                ident = len(posts)
                value = Decimal(quantity)*100
                orders[cid] = {'clientOrderId': cid, 'symbol': 'BTCUSDT', 'orderId': ident, 'side': side,
                    'origQty': quantity, 'executedQty': quantity, 'cummulativeQuoteQty': str(value), 'updateTime': ident, 'status': 'FILLED'}
                fills[ident] = [{'id': ident, 'orderId': ident, 'symbol': 'BTCUSDT', 'isBuyer': side=='BUY',
                    'qty': quantity, 'quoteQty': str(value), 'commission': str(value*Decimal('.001')), 'commissionAsset': 'USDT'}]
                # Simulate accepted by exchange but response lost on first entry.
                if ident == 1: raise TimeoutError()
                return {'orderId': ident, 'clientOrderId': cid}
            if path.endswith('/order'): return copy.deepcopy(orders[params['origClientOrderId'][0]])
            if path.endswith('/myTrades'): return copy.deepcopy(fills[int(params['orderId'][0])])
            raise AssertionError(path)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'testnet.db'
            exchange = TestnetExchange('FAKE', 'FAKE', enabled=True, transport=transport)
            store = RuntimeStore(path, {'mode':'testnet'})
            app = Application(store, exchange, exchange)
            store.command(1, True, '/resume')
            app.execute('BUY', '.1', Decimal(100), 'entry', 'TEST')
            self.assertEqual(store.pending()[0]['phase'], 'UNKNOWN')
            store.close()
            store = RuntimeStore(path, {'mode':'testnet'})
            try:
                app = Application(store, exchange, exchange)
                app.recover()
                pos = store.get('position')
                app.execute('SELL', pos['quantity'], Decimal(100), 'close', 'TEST', pos['id'])
                app.recover()
                self.assertEqual(len(posts), 2)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM runtime_fills').fetchone()[0], 2)
                self.assertFalse(store.pending())
                self.assertEqual(Decimal(store.get('pnl')), Decimal('-.02'))
                self.assertTrue(store.get('paused'))
            finally: store.close()
