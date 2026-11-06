import sqlite3,unittest
from research.adaptive_market_history import ingest

class MarketHistoryTest(unittest.TestCase):
 def setUp(self):self.db=sqlite3.connect(':memory:',isolation_level=None);self.addCleanup(self.db.close)
 def test_replay_and_revisions_preserve_original_and_block(self):
  data={'market':'spot','symbol':'BTCUSDT','source':'binance_spot_klines','bars':[(0,100.,101.,99.,100.)],'skip_bar':(900000,100.,101.,99.,100.)}
  self.assertFalse(ingest(self.db,market='spot',candles=data,observed_ms=1800000)['entry_blocked'])
  self.assertFalse(ingest(self.db,market='spot',candles=data,observed_ms=1800001)['entry_blocked'])
  changed={**data,'bars':[(0,100.,102.,99.,100.)]}
  self.assertTrue(ingest(self.db,market='spot',candles=changed,observed_ms=1800002)['entry_blocked'])
  self.assertEqual(self.db.execute('SELECT count(*) FROM market_conflicts').fetchone()[0],1)
  self.assertIn('101.0',self.db.execute('SELECT payload FROM market_candles WHERE start_ms=0').fetchone()[0])
 def test_gaps_and_cross_market_sources_are_rejected(self):
  data={'market':'spot','symbol':'BTCUSDT','source':'binance_usdm_klines','bars':[(0,100.,101.,99.,100.)],'skip_bar':(1800000,100.,101.,99.,100.)}
  with self.assertRaises(ValueError):ingest(self.db,market='spot',candles=data,observed_ms=2700000)
 def test_warm_and_skip_candles_keep_distinct_first_receipt_times(self):
  data={'market':'spot','symbol':'BTCUSDT','source':'binance_spot_klines','bars':[(0,100.,101.,99.,100.)],'skip_bar':(900000,100.,101.,99.,100.),'warm_received_ms':1000000}
  ingest(self.db,market='spot',candles=data,observed_ms=1800000)
  self.assertEqual(self.db.execute('SELECT first_received_ms FROM market_candles ORDER BY start_ms').fetchall(),[(1000000,),(1800000,)])
