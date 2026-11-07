import unittest
from research.usdm_book_capture import capture,public
class CaptureTests(unittest.TestCase):
 def test_snapshot_keeps_source_and_receipt_times_distinct(self):
  def fetch(path,params):
   if path.endswith('bookTicker'):return {'symbol':'BTCUSDT','bidPrice':'100','askPrice':'101','E':12}
   if path.endswith('depth'):return {'bids':[['100','1']],'asks':[['101','2']]}
   return [{'T':11,'p':'100'}]
  result=capture(fetch=fetch,clock=lambda:20)
  self.assertEqual(result['snapshots']['book']['data']['E'],12)
  self.assertEqual(result['snapshots']['book']['received_ms'],20)
  self.assertEqual(result['snapshots']['book']['source_time_status'],'reported_unverified')
  self.assertEqual(result['snapshots']['depth']['source_time_status'],'absent')
  self.assertNotIn('E',result['snapshots']['depth']['data'])
 def test_private_and_wrong_symbol_refused_without_network(self):
  for path,params in [('/fapi/v1/order',{'symbol':'BTCUSDT'}),('/fapi/v1/depth',{'symbol':'ETHUSDT'})]:
   with self.assertRaisesRegex(ValueError,'denied'):public(path,params)
if __name__=='__main__':unittest.main()
