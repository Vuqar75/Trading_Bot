import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import interactive_market_once as once
from trading_bot.market_research import ResearchError
from trading_bot.research_api_budget import _connect


class InteractiveMarketOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        original_cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, original_cwd)
        fixture = Path(once.__file__).resolve().parents[1] / 'research/fixtures/ai-observer-synthetic.json'
        self.raw = json.loads(fixture.read_text(encoding='utf-8'))

    def test_no_terminal_refuses_before_input_or_network(self):
        with patch.object(once.sys.stdin, 'isatty', return_value=False), \
             patch.object(once.getpass, 'getpass') as entry, \
             patch.object(once, 'capture_public') as capture:
            with self.assertRaisesRegex(ResearchError, 'interactive_terminal_required'):
                once.run(self.home)
        entry.assert_not_called()
        capture.assert_not_called()

    def test_secret_stays_out_of_files_and_repeat_never_calls_api(self):
        secret = 'fixture-secret-not-a-real-key'
        output = io.StringIO()
        def fake_paid(snapshot, budget, *, enabled):
            self.assertTrue(enabled)
            self.assertEqual(os.environ.get('TRADING_BOT_AI'), secret)
            self.assertNotIn('OPENAI_API_KEY', os.environ)
            return {'usage': {'input_tokens': 1}, 'budget': {'reserved_micro_usd': 1}}
        with patch.object(once.sys.stdin, 'isatty', return_value=True), \
             patch.object(once.getpass, 'getpass', return_value=secret), \
             patch.object(once, 'capture_public', return_value=self.raw), \
             patch.object(once, 'paid_analysis', side_effect=fake_paid) as paid, \
             patch.dict(os.environ, {'OPENAI_API_KEY': 'other-fixture', 'TRADING_BOT_AI': 'prior-fixture'}), \
             contextlib.redirect_stdout(output):
            ident = once.run(self.home)
            self.assertEqual(os.environ['OPENAI_API_KEY'], 'other-fixture')
            self.assertEqual(os.environ['TRADING_BOT_AI'], 'prior-fixture')
            with self.assertRaisesRegex(ResearchError, 'prior_attempt_requires_inspection'):
                once.run(self.home)
        self.assertEqual(paid.call_count, 1)
        self.assertNotIn(secret, output.getvalue())
        for path in self.home.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))
        self.assertTrue((self.home / 'research_results/ai-public' / ident / 'model-analysis.json').exists())

    def test_failed_attempt_keeps_gate_and_never_retries(self):
        with patch.object(once.sys.stdin, 'isatty', return_value=True), \
             patch.object(once.getpass, 'getpass', return_value='fixture-key'), \
             patch.object(once, 'capture_public', return_value=self.raw), \
             patch.object(once, 'paid_analysis', side_effect=ResearchError('fixture_failure')) as paid, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ResearchError, 'fixture_failure'):
                once.run(self.home)
            with self.assertRaisesRegex(ResearchError, 'prior_attempt_requires_inspection'):
                once.run(self.home)
        self.assertEqual(paid.call_count, 1)

    def test_failed_capture_does_not_claim_an_api_attempt(self):
        with patch.object(once.sys.stdin, 'isatty', return_value=True), \
             patch.object(once.getpass, 'getpass', return_value='fixture-key'), \
             patch.object(once, 'capture_public', side_effect=ResearchError('fixture_unavailable')), \
             patch.object(once, 'paid_analysis') as paid, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ResearchError, 'fixture_unavailable'):
                once.run(self.home)
        self.assertFalse((self.home / '.local/state/tradingbot-ai/interactive-once.lock').exists())
        paid.assert_not_called()

    def test_existing_budget_attempt_refuses_before_input_and_capture(self):
        budget = self.home / '.local/state/tradingbot-ai/experiment-budget.db'
        db = _connect(budget)
        try:
            db.execute("INSERT INTO api_attempts VALUES ('prior', 20, 1, NULL)")
        finally:
            db.close()
        with patch.object(once.sys.stdin, 'isatty', return_value=True), \
             patch.object(once.getpass, 'getpass') as entry, \
             patch.object(once, 'capture_public') as capture:
            with self.assertRaisesRegex(ResearchError, 'existing_attempt_requires_inspection'):
                once.run(self.home)
        entry.assert_not_called()
        capture.assert_not_called()

    def test_echo_fallback_is_not_allowed_to_reach_capture(self):
        with patch.object(once.sys.stdin, 'isatty', return_value=True), \
             patch.object(once.getpass, 'getpass', side_effect=once.getpass.GetPassWarning('no echo control')), \
             patch.object(once, 'capture_public') as capture:
            with self.assertRaises(once.getpass.GetPassWarning):
                once.run(self.home)
        capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
