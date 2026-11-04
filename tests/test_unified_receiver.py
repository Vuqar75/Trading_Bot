import tempfile
import unittest
from pathlib import Path
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.unified_receiver import MarketReceiver


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'spot.db'
        self.store = RuntimeStore(self.path, {'mode':'paper'})
        self.addCleanup(self.store.close)
        self.receiver = MarketReceiver(self.path, 'spot')
        self.addCleanup(self.receiver.close)

    def test_uncertain_commit_retry_is_idempotent(self):
        def fault(point):
            if point == 'after_commit': raise RuntimeError('crash')
        self.receiver.fault = fault
        with self.assertRaises(RuntimeError): self.receiver.apply(7, '/pause')
        self.receiver.close(); self.receiver = MarketReceiver(self.path, 'spot')
        self.assertEqual(self.receiver.apply(7, '/pause'), 'Входы на паузе; контроль позиции продолжается.')
        with self.assertRaises(ValueError): self.receiver.apply(7, '/resume')

    def test_rollback_before_commit_leaves_no_effect(self):
        with self.store.transaction(): self.store.set('paused', False)
        def fault(point):
            if point == 'before_commit': raise RuntimeError('crash')
        self.receiver.fault = fault
        with self.assertRaises(RuntimeError): self.receiver.apply(8, '/pause')
        self.assertFalse(self.store.get('paused'))
        self.receiver.fault = lambda _: None
        self.receiver.apply(8, '/pause')
        self.assertTrue(self.store.get('paused'))

    def test_confirmed_close_rechecks_quantity_position_and_expiration(self):
        with self.store.transaction():
            p = self.store.get('position'); self.store.set('position', {**p, 'quantity':'0.2'})
        p = self.store.get('position')
        bad = {'position':p['id'], 'quantity':'0.1', 'expires':100}
        self.assertIn('отклонено', self.receiver.apply(1, '/confirmed_close', bad, now=1))
        bad['quantity']='0.2'
        self.assertIn('отклонено', self.receiver.apply(2, '/confirmed_close', bad, now=100))
        bad['position']='different'
        self.assertIn('отклонено', self.receiver.apply(3, '/confirmed_close', bad, now=1))
        bad['position']=p['id']
        self.assertIn('сохранён', self.receiver.apply(4, '/confirmed_close', bad, now=1))
        self.assertEqual(self.store.get('close_request')['quantity'], '0.2')

    def test_receiver_rejects_other_market_schema(self):
        with self.assertRaises(ValueError):
            MarketReceiver(self.path, 'futures')

    def test_futures_pause_atomic(self):
        path = Path(self.tmp.name)/'futures.db'
        f = FuturesStore(path); self.addCleanup(f.close)
        with f.transaction(): f.set('paused', False)
        receiver = MarketReceiver(path, 'futures'); self.addCleanup(receiver.close)
        receiver.apply(5, '/pause')
        self.assertTrue(f.get('paused'))
