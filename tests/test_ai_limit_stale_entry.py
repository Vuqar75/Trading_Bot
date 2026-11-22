"""Synthetic timing reproduction of the contiguous stale SHORT batch."""
from tests import test_ai_limit_stream as fixtures
from tests.test_ai_limit_brackets import snapshot


class RetainedSubmitTests(fixtures.StreamTests):
    def short_entry(self):
        feed = self.stream()
        self.bootstrap(feed)
        self.send(feed, 2)
        self.poller.cycle()
        market, decision = snapshot("SHORT")
        self.book.accept(market, decision, provenance={
            "kind": "fixture", "response_id": "short-recovery", "model": "fixture"
        })
        return feed

    def send_short(self, feed, identity, age, price):
        feed.receive("BTCUSDT", {"e": "aggTrade", "s": "BTCUSDT", "a": identity,
                                "T": self.now-age, "p": price, "q": "1", "m": False})

    def stale_packet(self, feed, stale_price="100.8", fresh_price="100.9", first=3):
        self.now += 5400
        self.send_short(feed, first, 5232, stale_price)
        for i in range(42):
            self.send_short(feed, first+i+1, 352+41-i, fresh_price)
        return self.poller.cycle()

    def test_contiguous_noncrossing_packet_keeps_entry_until_later_fresh_cross(self):
        feed = self.short_entry()
        plan, state = self.book.active()
        reserve = state["reserved"]
        result = self.stale_packet(feed)
        report = result["trade_feeds"]["BTCUSDT"]
        self.assertEqual(report["entry_held_for_fresh_batch"], plan["plan_id"])
        self.assertEqual(report["stale_rows"], 1)
        self.assertEqual(report["rows"], 43)
        self.assertEqual(self.book.get("trade_cursor:BTCUSDT"), 45)
        self.assertIsNone(self.book.get("trade_confirmed_ms:BTCUSDT"))
        self.assertEqual(self.book.snapshot()["fills"], 0)
        self.assertEqual(self.book.active()[1]["reserved"], reserve)
        self.poller.cycle()
        self.assertIsNone(self.book.get("trade_confirmed_ms:BTCUSDT"))
        self.now += 1000
        self.send_short(feed, 46, 0, "101.1")
        self.poller.cycle()
        self.assertEqual(self.book.snapshot()["fills"], 1)
        self.assertIsNone(self.book.get("trade_held_through:BTCUSDT"))
        self.assertTrue(self.book.verify()["balanced"])

    def test_potential_cross_in_stale_packet_still_cancels(self):
        feed = self.short_entry()
        self.stale_packet(feed, stale_price="101.1")
        self.assertIsNone(self.book.active())
        self.assertEqual(self.book.plans()[0][1]["entry_cancellation"]["reason"], "TRADE_GAP")
        self.assertEqual(self.book.snapshot()["fills"], 0)
        self.assertEqual(self.book.balance("reserved"), 0)

    def test_contiguous_prices_do_not_excuse_an_id_gap(self):
        feed = self.short_entry()
        self.stale_packet(feed, first=4)
        self.assertIsNone(self.book.active())
        self.assertEqual(self.book.plans()[0][1]["entry_cancellation"]["reason"], "TRADE_GAP")
        self.assertEqual(self.book.snapshot()["fills"], 0)

    def test_entry_expires_at_original_deadline_during_hold(self):
        feed = self.short_entry()
        expiry = self.book.active()[0]["expires_ms"]
        self.stale_packet(feed)
        self.now = expiry
        self.poller.cycle()
        self.assertIsNone(self.book.active())
        self.assertEqual(self.book.plans()[0][1]["entry_cancellation"]["reason"], "EXPIRED_OR_PAUSED")
        self.assertEqual(self.book.balance("reserved"), 0)

    def test_cross_in_fresh_tail_of_discarded_packet_cancels(self):
        feed = self.short_entry()
        self.stale_packet(feed, fresh_price="101.1")
        self.assertIsNone(self.book.active())
        self.assertEqual(self.book.snapshot()["fills"], 0)

    def test_disconnect_during_hold_cancels_without_execution(self):
        feed = self.short_entry()
        self.stale_packet(feed)
        feed.fail("BTCUSDT", "disconnect")
        self.now += 1000
        self.send_short(feed, 46, 0, "101.1")
        self.poller.cycle()
        self.assertIsNone(self.book.active())
        self.assertEqual(self.book.snapshot()["fills"], 0)
        self.assertEqual(self.book.balance("reserved"), 0)

    def test_recovered_older_timestamp_cannot_execute_retained_entry(self):
        feed = self.short_entry()
        self.stale_packet(feed)
        self.now += 100
        self.send_short(feed, 46, 1000, "101.1")
        self.poller.cycle()
        self.assertIsNone(self.book.active())
        self.assertEqual(self.book.snapshot()["fills"], 0)
        self.assertEqual(self.book.balance("reserved"), 0)
