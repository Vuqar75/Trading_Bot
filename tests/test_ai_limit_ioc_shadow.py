import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from research.ai_limit_ioc_shadow import Capture, paired_result, replay


class IocShadowTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            'plan_id': 'accepted', 'symbol': 'BTCUSDT', 'direction': 'SHORT',
            'entry': '61000', 'stop': '61300', 'target': '59000', 'quantity': '0.001',
            'fee': '0.0005', 'funding_buffer_rate': '0.0021', 'stop_slippage': '0.001',
            'expires_ms': 15000, 'hold_ms': 300000,
            'snapshot': {'filters': {'tick_size': '0.1', 'step_size': '0.001',
                                     'min_qty': '0.001', 'min_notional': '50'}},
        }
        self.header = {'plan': self.plan, 'armed_ms': 10000,
                       'filters': self.plan['snapshot']['filters']}
        self.funding = {'from_ms': 0, 'through_ms': 1000000, 'events': []}

    def row(self, now, bid='60980', ask='60980.1', trades=(), stops=()):
        return {'at_ms': now, 'healthy': True, 'target_trades': list(trades),
                'stop_stamps': list(stops), 'filters_ready': True,
                'quote': {'id': str(now), 'symbol': 'BTCUSDT', 'bid': bid, 'ask': ask,
                          'mark': bid, 'bid_qty': '1', 'ask_qty': '1',
                          'event_ms': now, 'received_ms': now}}

    def test_time_after_passive_expiry_and_funding(self):
        rows = [self.row(t) for t in range(11000, 311001, 5000)]
        self.funding['events'].append({'symbol': 'BTCUSDT', 'event_ms': 20000,
                                       'rate': '0.0001', 'mark': '60980'})
        result = replay(self.header, rows, self.funding, Decimal(0))
        self.assertEqual((result['reason'], result['closed_ms']), ('TIME', 311000))
        self.assertEqual(Decimal(result['funding']), Decimal('0.006098'))
        self.assertEqual(Decimal(result['net']), Decimal(result['gross'])
                         - Decimal(result['fees']) + Decimal(result['funding']))

    def test_stop_uses_intermediate_component(self):
        rows = [self.row(11000), self.row(12000, stops=[11900])]
        result = replay(self.header, rows, self.funding, Decimal('0.0001'))
        self.assertEqual((result['reason'], result['entry']), ('SL', '60973.9'))

    def test_gap_never_produces_a_result(self):
        rows = [self.row(11000), self.row(17000)]
        self.assertEqual(replay(self.header, rows, self.funding, Decimal(0)),
                         {'status': 'unknown', 'reason': 'market_observation_gap'})

    def test_capture_persists_and_pairs_with_read_only_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'shadow.jsonl'
            capture = Capture(path)
            capture.start(self.plan, self.header['armed_ms'])
            trade = {'price': '58999.9', 'qty': '1', 'buyer_maker': True, 'event_ms': 11900}
            self.assertEqual(capture.target_trades([trade, dict(trade, buyer_maker=False)]), [trade])
            observations = [self.row(11000), self.row(12000, trades=[trade]), self.row(capture.end_ms)]
            for row in observations:
                capture.observe(row['at_ms'], row['quote'], row['target_trades'], True, filters_ready=True)
            capture.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[-1], {'complete': True})
            self.assertTrue(Capture(path).used)
            db = Path(temp) / 'paper.sqlite'
            with sqlite3.connect(db) as conn:
                conn.executescript('CREATE TABLE bracket_plans(id,state); CREATE TABLE bracket_meta(key,value);'
                                   'CREATE TABLE bracket_funding(payload);')
                state = {'status': 'CANCELED', 'gross': '0', 'fees': '0', 'funding': '0'}
                conn.execute('INSERT INTO bracket_plans VALUES (?,?)', ('accepted', json.dumps(state)))
                conn.executemany('INSERT INTO bracket_meta VALUES (?,?)',
                                 [('started_ms', '0'), ('funding_through', '1000000'), ('blocked', 'null')])
            before = db.read_bytes()
            result = paired_result(db, records[0], records[1:])
            self.assertEqual(result['base']['reason'], 'TP')
            self.assertEqual(result['base']['delta_net'], result['base']['net'])
            self.assertEqual(db.read_bytes(), before)

    def test_long_uses_ask_and_requires_full_volume(self):
        self.plan.update(direction='LONG', stop='60300', target='63000')
        opened = self.row(11000)
        opened['quote']['ask_qty'] = '0.0009'
        result = replay(self.header, [opened], self.funding, Decimal(0))
        self.assertEqual(result['status'], 'no_entry')
        opened['quote']['ask_qty'] = '1'
        target = self.row(12000, trades=[{'event_ms': 11900, 'qty': '1'}])
        result = replay(self.header, [opened, target], self.funding, Decimal(0))
        self.assertEqual((result['reason'], result['entry']), ('TP', '60980.1'))
