import unittest
import numpy as np
from research import hourly_ml as m
from research import extreme_reversal as e

class HourlyMLTest(unittest.TestCase):
 def test_fold_purges_labels_at_and_after_test_start(self):
  a=m.month_ms(2025,7);hour=60*e.M
  rows=[{'T':a-2*hour,'target':.1,'target_end_ms':a-hour+e.M},
        {'T':a-hour,'target':.1,'target_end_ms':a+e.M},
        {'T':a-3*hour,'target':.1,'target_end_ms':a},
        {'T':a,'target':None,'target_end_ms':a+61*e.M}]
  start,end,_,train,test=m.split_fold(rows,2025,7)
  self.assertEqual(start,m.month_ms(2025,1));self.assertEqual(end,a)
  self.assertEqual(len(train),1);self.assertEqual(len(test),1)

 def test_features_exclude_future_and_volume_median_excludes_current(self):
  h=210;rows=np.empty((h*60,5))
  for i in range(h):
   price=100+i*.1;rows[i*60:(i+1)*60]=[price,price+1,price-1,price,1]
  original,_=m.features(rows);t=original[0]['T'];first=original[0]['x']
  future=rows.copy();future[170*60:,0:4]*=2;future[168*60:169*60,4]=2
  changed,_=m.features(future);same=next(x for x in changed if x['T']==t)
  self.assertAlmostEqual(first[8],0.)
  self.assertAlmostEqual(same['x'][8],1.)
  self.assertEqual(first[:8],same['x'][:8]);self.assertEqual(first[9:],same['x'][9:])

 def test_threshold_and_short(self):
  self.assertIsNone(m.direction(.003749999,.00375))
  self.assertEqual(m.direction(-.00375,.00375),'SHORT')
  self.assertEqual(m.direction(.00375,.00375),'LONG')
  self.assertIsNone(m.direction(0.,0.))

 def test_one_hour_execution_and_duplicate(self):
  a=np.full((400,4),100.);T=e.START+100*e.M
  p={'symbol':'BTCUSDT','model':'XGBoost','T':T,'availability_ms':T+1000,'predicted':-.01,'actual':0.,'fold':'2025-07'}
  records,z=m.run_account([p,p],a,a,[],'BTCUSDT','XGBoost','base','threshold')
  self.assertEqual(records[0]['reason'],'time');self.assertEqual(records[0]['hold_min'],60)
  self.assertEqual(records[1]['rejection'],'duplicate_signal');self.assertEqual(z['closed'],1)
  t=records[0];self.assertAlmostEqual(t['gross']-t['fees']-t['impact']-t['liquidation_fee']+t['funding'],t['net'])

if __name__=='__main__':unittest.main()
