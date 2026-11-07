import unittest
from research.usdm_candle_timing import read_candle,PERIOD
class TimingTests(unittest.TestCase):
 def test_closed_and_raw(self):
  t=PERIOD+10
  def f(host,path,params):return {'serverTime':t} if path.endswith('/time') else [[0,'1','2','1','1','3',PERIOD-1]]
  r=read_candle(0,fetch=f,clock=lambda:t);self.assertTrue(r['closed_by_server_time']);self.assertEqual(r['raw'][5],'3')
 def test_open_or_wrong_symbol_boundary(self):
  t=PERIOD-1
  def f(host,path,params):return {'serverTime':t} if path.endswith('/time') else [[0,'1','2','1','1','3',PERIOD-1]]
  self.assertFalse(read_candle(0,fetch=f,clock=lambda:t)['closed_by_server_time'])
  with self.assertRaisesRegex(ValueError,'invalid_open'):read_candle(1,fetch=f,clock=lambda:t)
if __name__=='__main__':unittest.main()
