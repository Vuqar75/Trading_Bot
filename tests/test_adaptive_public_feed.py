import unittest
from research import adaptive_public_feed as f

class PublicFeedTest(unittest.TestCase):
 def test_prices_use_matching_instrument_and_source_event_time(self):
  calls=[]
  def fetch(host,path,params):
   calls.append((host,path,params))
   if host==f.SPOT:return [{'T':999,'p':'100'}]
   return {'symbol':'BTCUSDT','time':999,'markPrice':'101','nextFundingTime':2000}
  self.assertEqual(f.spot_quote(fetch=fetch,clock=lambda:1000)['price'],'100')
  self.assertEqual(f.futures_quote(fetch=fetch,clock=lambda:1000)['price'],'101')
  self.assertNotEqual(calls[0][0],calls[1][0])
 def test_stale_trade_and_missing_event_time_fail_closed(self):
  for row in ({'p':'100','T':1},{'p':'100'}):
   with self.assertRaises(ValueError):f.spot_quote(fetch=lambda *x:[row],clock=lambda:6000)
 def test_funding_history_is_bounded_and_preserves_observation(self):
  data=f.funding_history(100,200,fetch=lambda *x:[{'symbol':'BTCUSDT','fundingTime':150}],clock=lambda:201)
  self.assertEqual(data['received_ms'],201);self.assertEqual(data['rows'][0]['fundingTime'],150)
  with self.assertRaisesRegex(ValueError,'window_invalid'):f.funding_history(0,8*86400000,fetch=lambda *x:[],clock=lambda:0)
  with self.assertRaisesRegex(ValueError,'history_incomplete'):f.funding_history(100,200,fetch=lambda *x:[],clock=lambda:199)
 def test_filters_require_unique_usdm_perpetual_identity(self):
  contract={'symbol':'BTCUSDT','contractType':'PERPETUAL','marginAsset':'USDT','filters':[]}
  self.assertEqual(f.filters('futures',fetch=lambda *x:{'symbols':[contract]},clock=lambda:100)['received_ms'],100)
  for bad in (dict(contract,contractType='CURRENT_QUARTER'),dict(contract,marginAsset='BTC')):
   with self.assertRaisesRegex(ValueError,'contract_type_invalid'):f.filters('futures',fetch=lambda *x:{'symbols':[bad]},clock=lambda:100)
  with self.assertRaisesRegex(ValueError,'filters_unavailable'):f.filters('futures',fetch=lambda *x:{'symbols':[contract,contract]},clock=lambda:100)
 def test_private_or_non_allowlisted_paths_are_rejected_before_network(self):
  for host,path in ((f.SPOT,'/api/v3/order'),(f.FUTURES,'/fapi/v1/order'),('https://example.com','/api/v3/klines')):
   with self.assertRaisesRegex(ValueError,'endpoint_denied'):f.get(host,path,{'symbol':'BTCUSDT'})
 def test_closed_candles_use_requested_market_and_exclude_open_bar(self):
  now=20000*900000+1000;seen=[]
  def fetch(host,path,params):
   seen.append((host,path))
   if path.endswith('/time'):return {'serverTime':now}
   return [[t,'100','101','99','100','10',t+899999] for t in range(19801*900000,20001*900000,900000)]
  for market,host in (('spot',f.SPOT),('futures',f.FUTURES)):
   data=f.closed_15m(market,fetch=fetch,clock=lambda:now)
   self.assertEqual(data['skip_bar'][0],19999*900000)
   self.assertEqual(data['bars'][-1][0],19998*900000)
   self.assertEqual(seen[-2][0],host)
 def test_candle_boundary_cross_during_fetch_is_rejected(self):
  now=20000*900000+1000;count=[0]
  def fetch(host,path,params):
   if path.endswith('/time'):
    count[0]+=1;return {'serverTime':now if count[0]==1 else now+900000}
   return [[t,'100','101','99','100','10',t+899999] for t in range(19801*900000,20001*900000,900000)]
  with self.assertRaises(ValueError):f.closed_15m('spot',fetch=fetch,clock=lambda:now)
 def test_execution_uses_preboundary_history_and_postboundary_price(self):
  boundary=20000*900000;warm={'market':'spot','symbol':'BTCUSDT','source':'binance_spot_klines','received_ms':boundary-30000,'bars':[(boundary-2700000,100.,101.,99.,100.)],'skip_bar':(boundary-1800000,100.,101.,99.,100.)}
  price={'symbol':'BTCUSDT','source':'binance_spot_trade','price':'100','event_ms':boundary+300,'received_ms':boundary+400}
  raw=[[t,'100','101','99','100','10',t+899999] for t in (boundary-1800000,boundary-900000,boundary)]
  got=f.execution_candles('spot',warm,price,fetch=lambda *x:raw,clock=lambda:boundary+500)
  self.assertEqual(got['bars'][-1][0],boundary-1800000);self.assertEqual(got['skip_bar'][0],boundary-900000)
  with self.assertRaisesRegex(ValueError,'warm_history_stale'):f.execution_candles('spot',{**warm,'received_ms':boundary+1},price,fetch=lambda *x:raw,clock=lambda:boundary+500)
  with self.assertRaisesRegex(ValueError,'execution_window_missed'):f.execution_candles('spot',warm,{**price,'event_ms':boundary+6000,'received_ms':boundary+6000},fetch=lambda *x:raw,clock=lambda:boundary+6000)
