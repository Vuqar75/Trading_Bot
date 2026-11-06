import unittest
from research.adaptive_market_proof import quote

class MarketProofTest(unittest.TestCase):
 def test_spot_and_futures_sources_are_not_interchangeable(self):
  spot={'symbol':'BTCUSDT','source':'binance_spot_trade','price':'100','event_ms':999,'received_ms':1000}
  self.assertEqual(quote(spot,market='spot',now_ms=1001)['price'],'100')
  with self.assertRaisesRegex(ValueError,'source_or_symbol'):quote(spot,market='futures',now_ms=1001)
  mark={**spot,'source':'binance_usdm_mark'}
  self.assertEqual(quote(mark,market='futures',now_ms=1001)['price'],'100')
 def test_stale_future_and_missing_event_times_are_rejected(self):
  q={'symbol':'BTCUSDT','source':'binance_spot_trade','price':'100','event_ms':1000,'received_ms':1001}
  for data,now in ((q,6001),({**q,'event_ms':1002},1001),({**q,'event_ms':None},1001),({**q,'received_ms':1002},1001),({**q,'price':'NaN'},1001)):
   with self.assertRaises(ValueError):quote(data,market='spot',now_ms=now)
