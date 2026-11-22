import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from research import ai_limit_plan as plan
from research import ai_limit_runtime as runtime
from research.ai_limit_book import Book
from research.ai_limit_demo import book_quote, capture, fixture_decision
from tests.test_ai_limit_brackets import T


class TrendAlignmentTests(TestCase):
    def setup_context(self, slope=0, policy=plan.TREND_ALIGNMENT_POLICY, *, flat=False):
        cap = capture(T)
        for i, candle in enumerate(cap["candles"]):
            candle[1:5] = ["100"] * 4 if flat else [
                plan.text(plan.dec(v) + plan.dec(str(slope)) * i + plan.dec(str(max(0, -slope * 960)))) for v in candle[1:5]
            ]
        quote = book_quote(T, cap["candles"][-1][4])
        filters = dict(as_of_ms=T, tick_size=".1", step_size=".001", min_qty=".001", min_notional="50")
        news = dict(coverage="limited", high_risk_until_ms=0, items=[])
        return plan.context(cap, quote, filters, news, T, candidate_policy=policy), cap

    def test_aligned_trend_blocks_countertrend_model_decision(self):
        for slope, direction, opposite in ((.2, "LONG", "SHORT"), (-.04, "SHORT", "LONG")):
            snap, _ = self.setup_context(slope)
            self.assertEqual(snap["trend_alignment"]["allowed"], direction)
            self.assertTrue(snap["candidate_plans"])
            self.assertTrue(all(c["direction"] == direction for c in snap["candidate_plans"]))
            bad = dict(fixture_decision(snap, "WAIT", T), decision=opposite)
            with self.assertRaisesRegex(ValueError, "entry_against_trend_alignment"):
                plan.validate(snap, bad, T)

    def test_mixed_and_stale_frames_are_separate_outcomes(self):
        snap, _ = self.setup_context(.12)
        self.assertEqual(snap["trend_alignment"], {"1h": "MIXED", "4h": "UP", "allowed": None})
        self.assertEqual(snap["candidate_plans"], [])
        snap["timeframes"]["4h"]["closed_through_ms"] -= 16*plan.BAR
        snap["snapshot_id"] = plan.digest({k: v for k, v in snap.items() if k != "snapshot_id"})
        with self.assertRaisesRegex(ValueError, "trend_frames_unconfirmed"):
            plan.verify_context(snap, T)

    def test_new_policy_allows_aligned_plan_through_normal_ingest(self):
        snap, cap = self.setup_context(.2)
        decision = fixture_decision(snap, "LONG", T)
        envelope = dict(capture=cap, analysis=dict(
            snapshot=snap, decision=decision, admission={"allowed": True},
            provenance={"kind": "budget_response", "response_id": "aligned-long", "model": "fixture"}
        ))
        with tempfile.TemporaryDirectory() as tmp:
            book = Book(Path(tmp) / "book", clock=lambda: T)
            try:
                book.funding(T, T, [])
                book.pause(False)
                book.set("next_due:BTCUSDT", T+10000)
                self.assertEqual(runtime.ingest(book, envelope, require_trend_alignment=True), "ACCEPTED")
                self.assertEqual(book.active()[0]["direction"], "LONG")
                self.assertTrue(book.verify()["balanced"])
            finally:
                book.close()

    def test_inconsistent_context_is_rejected_even_with_new_hash(self):
        snap, _ = self.setup_context(.2)
        snap["trend_alignment"]["allowed"] = "SHORT"
        snap["snapshot_id"] = plan.digest({k: v for k, v in snap.items() if k != "snapshot_id"})
        with self.assertRaisesRegex(ValueError, "trend_alignment_mismatch"):
            plan.verify_context(snap, T)

    def test_unspecified_direction_publishes_program_wait_without_api(self):
        snap, cap = self.setup_context(flat=True)
        self.assertEqual(snap["candidate_plans"], [])
        self.assertIsNone(snap["trend_alignment"]["allowed"])
        feed = Mock()
        feed.snapshot.return_value = (snap, cap)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "public.json"
            output = root / "answer.json"
            runtime.atomic(public, dict(version=plan.VERSION, paper_only=True, ledger_balanced=True,
                                        as_of_ms=T, paused=False, blocked=None, plans=[], funding_through_ms=T))
            with patch.object(runtime, "once", side_effect=AssertionError("paid path")) as paid:
                result = runtime.analyst(root / "budget", public, output, symbol="BTCUSDT",
                                         paper_uid=os.geteuid(), feed=feed, clock=lambda: T)
                paid.assert_not_called()
            self.assertEqual(result["reason"], "trend_alignment_unconfirmed")
            self.assertFalse((root / "budget").exists())

    def test_required_policy_rejects_a_legacy_directional_envelope(self):
        snap, cap = self.setup_context(policy=plan.CANDIDATE_POLICY)
        decision = fixture_decision(snap, "SHORT", T)
        result = dict(snapshot=snap, decision=decision, admission={"allowed": True},
                      provenance={"kind": "budget_response", "response_id": "saved-short", "model": "fixture"})
        envelope = dict(analysis=result, capture=cap)
        with tempfile.TemporaryDirectory() as tmp:
            book = Book(Path(tmp) / "book", clock=lambda: T)
            try:
                self.assertEqual(runtime.ingest(book, envelope, require_trend_alignment=True), "REJECTED")
                self.assertEqual(book.get("analyst_last")["reason"], "trend_policy_required")
                self.assertEqual(book.snapshot()["plans"], [])
                self.assertTrue(book.verify()["balanced"])
            finally:
                book.close()
