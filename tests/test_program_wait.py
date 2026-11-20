import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from research import ai_limit_plan as plan
from research import ai_limit_runtime as runtime
from research.ai_limit_book import Book
from research.ai_limit_demo import book_quote, capture
from trading_bot.ai_bracket_view import render
from tests.test_ai_limit_brackets import T


class ProgramWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / 'envelope.json'
        self.public = self.root / 'orders.json'
        self.status = dict(version=plan.VERSION, paper_only=True, ledger_balanced=True, as_of_ms=T,
                           paused=False, blocked=None, plans=[], funding_through_ms=T)
        runtime.atomic(self.public, self.status)
        self.cap = capture(T)
        for candle in self.cap['candles']:
            candle[1:5] = ['100'] * 4
        self.filters = dict(as_of_ms=T, tick_size='.1', step_size='.001', min_qty='.001', min_notional='50')
        self.snap = plan.context(self.cap, book_quote(T), self.filters,
                                 dict(coverage='limited', high_risk_until_ms=0, items=[]), T)
        self.assertEqual(self.snap['candidate_plans'], [])

    def invoke(self, snap):
        feed = Mock()
        feed.snapshot.return_value = (snap, self.cap)
        return runtime.analyst(self.root / 'absent-budget', self.public, self.output, symbol='BTCUSDT',
                               paper_uid=os.geteuid(), feed=feed, clock=lambda: T)

    def test_wait_published_without_budget_or_trade_and_replay_is_idempotent(self):
        with patch.object(runtime, 'once', side_effect=AssertionError('paid path')) as paid:
            self.assertFalse(self.invoke(self.snap)['model_requested'])
            paid.assert_not_called()
        self.assertFalse((self.root / 'absent-budget').exists())
        envelope = json.loads(self.output.read_text())
        book = Book(self.root / 'book', clock=lambda: T)
        try:
            before = book.snapshot()
            self.assertEqual(runtime.ingest(book, envelope), 'WAIT')
            self.assertEqual(runtime.ingest(book, envelope), 'already_ingested')
            after = book.snapshot()
            self.assertEqual(after['balances'], before['balances'])
            self.assertEqual(after['api_summary'], before['api_summary'])
            self.assertEqual(after['plans'], [])
            self.assertEqual(after['analyst_last']['provenance']['snapshot_id'], self.snap['snapshot_id'])
            runtime.atomic(self.public, after)
            self.assertIn('программный WAIT', render(self.public, now=T/1000))
        finally:
            book.close()

    def test_missing_list_is_not_empty_and_noncandidates_cannot_create_a_plan(self):
        with patch.object(runtime, 'once') as paid:
            for value in (None, {}, ''):
                with self.assertRaisesRegex(ValueError, 'candidate_list_missing_or_invalid'):
                    self.invoke(dict(self.snap, candidate_plans=value))
            paid.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'model_envelope_shape'):
            runtime.ingest(None, {'program_wait': self.snap, 'capture': self.cap, 'decision': 'LONG'})

    def test_nonempty_uses_existing_paid_path(self):
        snapshot = plan.context(capture(T), book_quote(T), self.filters, self.snap['news'], T)
        self.assertTrue(snapshot['candidate_plans'])
        with patch.object(runtime, 'once', return_value={'admission': {'allowed': False},
                                                         'decision': {'decision': 'WAIT'}}) as paid:
            self.invoke(snapshot)
            paid.assert_called_once()
        self.assertIn('analysis', json.loads(self.output.read_text()))

    def test_invalid_capture_nonempty_or_older_wait_cannot_replace_later_status(self):
        book = Book(self.root / 'book', clock=lambda: T+1000)
        try:
            other = capture(T)
            nonempty = plan.context(other, book_quote(T), self.filters, self.snap['news'], T)
            with self.assertRaisesRegex(ValueError, 'invalid_program_wait'):
                runtime.ingest(book, {'program_wait': nonempty, 'capture': other})
            with self.assertRaisesRegex(ValueError, 'source_context_recomputation_failed'):
                runtime.ingest(book, {'program_wait': self.snap, 'capture': other})
            newer = dict(as_of_ms=T+500, status='WAIT', reason='newer')
            with book.transaction():
                book.set('analyst_last', newer)
            runtime.ingest(book, {'program_wait': self.snap, 'capture': self.cap})
            self.assertEqual(book.get('analyst_last'), newer)
            self.assertEqual(book.snapshot()['plans'], [])
        finally:
            book.close()
