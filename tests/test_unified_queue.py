import tempfile
import unittest
from pathlib import Path
from trading_bot.unified_queue import UnifiedQueue


class UnifiedQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'queue.db'
        self.q = UnifiedQueue(self.path)
        self.addCleanup(lambda: self.q.close())

    def test_market_frozen_at_accept_and_duplicate(self):
        self.q.receive(10, True, '/pause')
        self.q.receive(11, True, '/mode futures')
        self.q.receive(10, True, '/pause')
        self.assertEqual(len(self.q.pending('spot')), 1)
        self.assertEqual(len(self.q.pending('futures')), 0)
        self.assertEqual(self.q.state('mode'), 'futures')
        self.assertEqual(self.q.state('offset'), '12')

    def test_report_bound_to_selected_market_balance_report_is_global(self):
        self.q.receive(1,True,'/mode futures_cross')
        self.q.receive(2,True,'/report')
        self.q.receive(3,True,'/balance_report today')
        self.q.receive(4,True,'/mode spot')
        self.q.receive(5,True,'/report')
        self.assertEqual(self.q.pending('futures_cross')[0]['command'],'/report')
        self.assertEqual(self.q.pending('spot')[0]['command'],'/report')
        self.assertEqual(self.q.pending('wallet')[0]['command'],'/balance_report today')
        self.q.receive(6,True,'/help')
        text=self.q.db.execute('SELECT message FROM unified_outbox ORDER BY id DESC').fetchone()[0]
        self.assertIn('/report',text);self.assertIn('/balance_report',text)

    def test_foreign_sender_cannot_change_mode_or_enqueue(self):
        self.q.receive(1, False, '/mode futures')
        self.assertEqual(self.q.state('mode'), 'spot')
        self.assertFalse(self.q.pending('futures'))
        self.assertEqual(self.q.db.execute('select count(*) from unified_outbox').fetchone()[0], 0)

    def test_pause_all_partial_and_failed_does_not_claim_completion(self):
        self.q.receive(1, True, '/pause_all')
        a, b = self.q.pending('spot')[0], self.q.pending('futures')[0]
        self.q.acknowledge(a['id'], 'На паузе')
        self.q.acknowledge(b['id'], 'Не удалось', applied=False)
        messages = [r[0] for r in self.q.db.execute('select message from unified_outbox')]
        self.assertFalse(any('применены к обоим' in m for m in messages))
        self.assertTrue(any('futures' in m for m in messages))

    def test_market_unavailable_does_not_block_other_jobs(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(2, True, '/pause')
        self.q.receive(3, True, '/mode spot')
        self.q.receive(4, True, '/pause')
        self.q.acknowledge(self.q.pending('spot')[0]['id'], 'На паузе')
        self.assertEqual(len(self.q.pending('futures')), 1)
        self.assertFalse(self.q.pending('spot'))

    def test_close_confirmation_uses_proposal_market_and_identity(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(2, True, '/close', positions={'futures':{'id':'position-a','quantity':'0.123'}})
        token = self.q.add_proposal(2, 'futures', 'position-a', '0.123', now=10)
        self.q.receive(3, True, '/mode spot')
        self.q.receive(4, True, '/confirm '+token, now=11)
        job = self.q.pending('futures')[0]
        self.assertEqual(job['command'], '/confirmed_close')
        self.assertIn('position-a', job['payload'])
        self.assertIn('0.123', job['payload'])
        self.q.receive(5, True, '/confirm '+token, now=12)
        self.assertEqual(len(self.q.pending('futures')), 1)

    def test_expired_and_canceled_confirmation_are_rejected(self):
        self.q.receive(1, True, '/close', positions={'spot':{'id':'p','quantity':'1'}})
        token = self.q.add_proposal(1, 'spot', 'p', '1', now=10, ttl=5)
        self.q.receive(2, True, '/confirm '+token, now=15)
        self.assertFalse(self.q.pending('spot'))
        self.q.receive(3, True, '/close', positions={'spot':{'id':'p','quantity':'1'}})
        token = self.q.add_proposal(3, 'spot', 'p', '1', now=20)
        self.q.receive(4, True, '/cancel '+token, now=21)
        self.q.receive(5, True, '/confirm '+token, now=22)
        self.assertFalse(self.q.pending('spot'))

    def test_reopen_preserves_mode_and_job(self):
        self.q.receive(1, True, '/mode futures')
        self.q.receive(5, True, '/pause')
        self.q.close()
        self.q = UnifiedQueue(self.path)
        self.assertEqual(self.q.state('mode'), 'futures')
        self.assertEqual(self.q.pending('futures')[0]['update_id'], 5)

    def test_rollback_does_not_advance_offset_or_store_snapshot(self):
        def fault(where):
            if where == 'before_commit': raise RuntimeError('crash')
        self.q.fault = fault
        with self.assertRaises(RuntimeError): self.q.receive(1, True, '/mode futures')
        self.q.fault = lambda _: None
        self.assertEqual(self.q.state('offset'), '0')
        self.assertEqual(self.q.state('mode'), 'spot')
        self.q.receive(1, True, '/mode futures')
        self.assertEqual(self.q.state('offset'), '2')

    def test_conflicting_duplicate_rejected(self):
        self.q.receive(7, True, '/pause')
        with self.assertRaises(ValueError): self.q.receive(7, True, '/resume')

    def test_retry_after_freshness_changes_does_not_change_snapshot(self):
        self.q.receive(1, True, '/resume', fresh=True)
        self.q.receive(1, True, '/resume', fresh=False)
        self.assertEqual(len(self.q.pending('spot')), 1)
