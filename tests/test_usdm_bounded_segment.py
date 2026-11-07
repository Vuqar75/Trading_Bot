import os,tempfile,unittest
from research.usdm_bounded_segment import collect
class CaptureTests(unittest.TestCase):
 def test_deadline_gaps_exclusive(self):
  clock=[0.]
  def sleep(s):clock[0]+=s
  def sample():
   if clock[0]>=5:raise ValueError('unavailable')
   return {'test':True}
  with tempfile.TemporaryDirectory() as d:
   path=os.path.join(d,'segment')
   result=collect(path,duration=10,clock=lambda:clock[0],wall=lambda:42,sleep=sleep,sample=sample)
   self.assertEqual((result['snapshots'],result['gaps']),(1,1))
   with self.assertRaises(FileExistsError):collect(path,duration=1,clock=lambda:clock[0],sample=sample)
 def test_bounds(self):
  self.assertRaises(ValueError,collect,'unused',duration=1801)
  self.assertRaises(ValueError,collect,'unused',interval=4)
if __name__=='__main__':unittest.main()
