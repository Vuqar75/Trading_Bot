import unittest
from research.usdm_screen import H,aggregates,signals,simulate

STEP=300000
def candle(i,o=100,h=100.1,l=99.9,c=100,v=10):return (i*STEP,o,h,l,c,v)
def event(*,long=False,short=False,exitlong=False,exitshort=False):
 return {'A':(long,short,exitlong,exitshort),'B':(False,False,False,False),'C':(False,False,False,False),'D':(False,False,False,False),'lo':90,'hi':110,'mid':100,'regime':'trend'}
class UsdmScreenTests(unittest.TestCase):
 def test_aggregation_and_no_future_lookahead(self):
  rows=[candle(i) for i in range(160)];out=list(aggregates(rows,STEP,15*60000));self.assertEqual((len(out),out[0][0],out[0][5]),(53,0,30))
  changed=rows.copy();changed[150]=candle(150,100,110,99,105)
  a=signals(rows,STEP,'5m');b=signals(changed,STEP,'5m')
  self.assertEqual({k:v for k,v in a.items() if k<150},{k:v for k,v in b.items() if k<150})
 def test_short_funding_and_fee_from_full_notional(self):
  rows=[candle(0),candle(1),candle(2,99.5,99.8,99.2,99.5),candle(3,99,99.2,98.8,99),candle(4,99,99.2,98.8,99)]
  marks=list(rows);ev={0:event(short=True),2:event(exitshort=True)};fund={1:[(STEP+1,.001)]}
  x=simulate(rows,marks,fund,ev,'BTCUSDT','5m','A','SHORT',0,5*STEP,impact=0,fee_bps=5)
  self.assertEqual(x['closed'],1);self.assertGreater(x['net'],0);self.assertGreater(x['funding'],0)
  self.assertAlmostEqual(x['fees'],x['turnover']*.0005,places=10)
  self.assertAlmostEqual(x['net'],x['gross']+x['funding']-x['fees']-x['impact'],places=9)
 def test_stale_gap_and_funding_boundary(self):
  rows=[candle(i) for i in range(5)];marks=rows.copy();marks[1]=candle(2)
  with self.assertRaisesRegex(ValueError,'trade/mark'):simulate(rows,marks,{}, {0:event(long=True)},'BTCUSDT','5m','A','LONG',0,5*STEP)
 def test_mark_liquidation_precedes_trade_stop(self):
  rows=[candle(0),candle(1,100,101,98,99),candle(2)]
  marks=[candle(0),candle(1,100,101,.001,99),candle(2)]
  x=simulate(rows,marks,{}, {0:event(long=True)},'BTCUSDT','5m','A','LONG',0,3*STEP,impact=0)
  self.assertEqual(x['liquidations'],1);self.assertEqual(x['exit_reasons'],{'liquidation':1});self.assertGreater(x['fees'],.09)
if __name__=='__main__':unittest.main()
