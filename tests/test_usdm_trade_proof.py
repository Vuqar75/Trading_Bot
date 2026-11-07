import unittest
from research.adaptive_market_proof import futures_trade_quote
from research.adaptive_public_feed import futures_execution_trade
class TradeProofTests(unittest.TestCase):
 def test_live_shape_and_source(self):
  q=futures_execution_trade(not_before_ms=1000,clock=lambda:1003,fetch=lambda h,p,k:[{'p':'76000.1','T':1002,'a':42}])
  self.assertEqual(q['source'],'binance_usdm_trade');self.assertEqual(q['event_ms'],1002)
  with self.assertRaisesRegex(ValueError,'trade_source_invalid'):futures_trade_quote(dict(q,source='binance_usdm_mark'),now_ms=1003,not_before_ms=1000)
 def test_past_future_stale(self):
  q={'source':'binance_usdm_trade','symbol':'BTCUSDT','trade_id':42,'price':'76000','event_ms':999,'received_ms':1001}
  with self.assertRaisesRegex(ValueError,'predates'):futures_trade_quote(q,now_ms=1001,not_before_ms=1000)
  with self.assertRaisesRegex(ValueError,'future'):futures_trade_quote(dict(q,event_ms=1002),now_ms=1001,not_before_ms=1000)
  with self.assertRaisesRegex(ValueError,'stale'):futures_trade_quote(dict(q,event_ms=1000,received_ms=1001),now_ms=7001,not_before_ms=1000)
if __name__=='__main__':unittest.main()
