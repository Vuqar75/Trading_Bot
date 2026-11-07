import copy,unittest
from research.usdm_study_sizing import minimum_futures_size
INFO={'symbol':'BTCUSDT','baseAsset':'BTC','quoteAsset':'USDT','marginAsset':'USDT','contractType':'PERPETUAL','marketTakeBound':'0.03','status':'TRADING','orderTypes':['MARKET'],'filters':[{'filterType':'LOT_SIZE','minQty':'0.001','maxQty':'1000','stepSize':'0.001'},{'filterType':'MARKET_LOT_SIZE','minQty':'0.001','maxQty':'1000','stepSize':'0.001'},{'filterType':'MIN_NOTIONAL','notional':'50'}]}
def size(info=INFO,price='76000',stop='0.02'):return minimum_futures_size(info,price,stop_distance=stop,fee_bps=10,slippage_bps=5)
class SizingTests(unittest.TestCase):
 def test_minimal(self):
  v=size();self.assertEqual(v['quantity'],'0.001');self.assertEqual(v['reference_notional'],'76.000');self.assertLess(float(v['risk_usdt']),5)
  self.assertEqual(v['funding_stress_bps'],'210');self.assertGreater(float(v['risk_usdt']),3)
 def test_notional_cap(self):
  with self.assertRaisesRegex(ValueError,'notional_limit'):size(price='101000')
 def test_market_minimum_uses_mark_not_trade_price(self):
  # At a 0.001 BTC step, a 49,999 USDT mark requires 0.002 BTC.
  result=minimum_futures_size(INFO,'50000',filter_price='49999',stop_distance='.02',fee_bps=10,slippage_bps=5)
  self.assertEqual(result['quantity'],'0.002');self.assertEqual(result['execution_notional_usdt'],'100.000')
 def test_risk_cap(self):
  with self.assertRaisesRegex(ValueError,'risk_limit'):size(stop='0.09')
 def test_market_take_bound(self):
  with self.assertRaisesRegex(ValueError,'market_take_bound_exceeded'):minimum_futures_size(INFO,'76000',filter_price='70000',stop_distance='.02',fee_bps=10,slippage_bps=5)
 def test_market_step_and_min(self):
  x=copy.deepcopy(INFO);x['filters'][1]['stepSize']='0.002'
  with self.assertRaisesRegex(ValueError,'notional_limit'):size(x)
 def test_duplicate_and_contract(self):
  x=copy.deepcopy(INFO);x['filters'].append(x['filters'][0]);self.assertRaises(ValueError,size,x)
  x=copy.deepcopy(INFO);x['symbol']='ETHUSDT';self.assertRaises(ValueError,size,x)
if __name__=='__main__':unittest.main()
