import unittest
from research.usdm_segment_analysis import analyse,classify_snapshot_ids

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
 def test_snapshot_omission_is_not_stream_failure(self):
  a=s(10000);a['value']['snapshots']['trades']['data']=[{'a':1},{'a':2}]
  b=s(15000);b['value']['snapshots']['trades']['data']=[{'a':5},{'a':6}]
  out=classify_snapshot_ids([a,b]);self.assertEqual(out['snapshot_intervals_with_omissions'],1);self.assertEqual(out['omitted_ids_between_snapshots'],2);self.assertFalse(out['continuous_stream_promised'])
 def test_duplicates_order_and_capture_gap_are_distinct(self):
  a=s(10000);a['value']['snapshots']['trades']['data']=[{'a':2},{'a':1},{'a':1}]
  b=s(15000,100);out=classify_snapshot_ids([a,{'kind':'gap'},b])
  self.assertEqual(out['duplicates_within_snapshots'],1);self.assertEqual(out['out_of_order_pairs_within_snapshots'],1);self.assertEqual(out['capture_gap_records'],1);self.assertEqual(out['snapshot_intervals_with_omissions'],0)
 def test_internal_gap_is_separate(self):
  a=s(10000);a['value']['snapshots']['trades']['data']=[{'a':1},{'a':4}]
  out=classify_snapshot_ids([a]);self.assertEqual(out['gap_pairs_within_snapshots'],1);self.assertEqual(out['snapshot_intervals_with_omissions'],0)
if __name__=='__main__':unittest.main()
