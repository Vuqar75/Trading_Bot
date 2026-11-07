import unittest
from research.usdm_segment_analysis import analyse

def s(t,a=1):
 def x(data):return {'data':data,'received_ms':t,'source_event_ms':t-1,'source_time_status':'reported_unverified'}
 return {'kind':'snapshot','value':{'snapshots':{'book':x({'bidPrice':'999.9','askPrice':'1000.1'}),'depth':x({'bids':[['999.9','10']],'asks':[['1000.1','10']]}),'trades':x([{'a':a}])}}}
class SegmentTests(unittest.TestCase):
 def test_cost_and_gap(self):
  out=analyse([s(10000),s(15000,4)],min_notional=50,step=.01,cap=100)
  self.assertEqual(out['gaps_detected'],1);self.assertAlmostEqual(out['spread_bps']['p50'],2);self.assertAlmostEqual(out['model_round_trip_bps_min']['p50'],12)
 def test_future_and_gap(self):
  x=s(10000);x['value']['snapshots']['book']['source_event_ms']=10001
  out=analyse([x,{'kind':'gap'}]);self.assertEqual(out['unusable_time'],1);self.assertEqual(out['gaps_detected'],1)
if __name__=='__main__':unittest.main()
