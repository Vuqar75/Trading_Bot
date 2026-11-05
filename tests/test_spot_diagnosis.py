import datetime as dt
import unittest
from research.spot_diagnosis import diagnostic
from research.spot_hypothesis import bull_filter,filtered
from research.strategy_study import PERIOD,simulate

class SpotDiagnosticTests(unittest.TestCase):
    def test_independent_gross_cost_and_previous_simulator(self):
        start=int(dt.datetime(2025,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
        rows=[(start,100,100,100,100),(start+PERIOD,110,110,110,110)]
        got=diagnostic(rows,[1,0],'ETHUSDT',start,start+2*PERIOD)
        # Entry and exit references are both 110, q rounded to 0.0908 ETH.
        q=.0908;gross=0.;spread=q*110*.001;fees=q*110*(1.0005+.9995)*.001
        self.assertAlmostEqual(got['gross'],gross,places=10)
        self.assertAlmostEqual(got['spread_slippage'],spread,places=10)
        self.assertAlmostEqual(got['fees'],fees,places=10)
        self.assertAlmostEqual(got['net'],gross-spread-fees,places=9)
        old=simulate(rows,[1,0],['unknown']*2,'ETHUSDT',2025)
        self.assertAlmostEqual(got['net'],old['net'],places=9)
        self.assertAlmostEqual(got['open_end_pnl'],got['net'],places=9)

    def test_filter_does_not_use_current_or_future_day(self):
        start=int(dt.datetime(2025,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
        rows=[]
        for i in range(34*96):
            price=100. if i<32*96 else 120.
            rows.append((start+i*PERIOD,price,price,price,price))
        altered=rows[:32*96]+[(t,1,1,1,1) for t,*_ in rows[32*96:]]
        self.assertEqual(bull_filter(rows)[:33*96],bull_filter(altered)[:33*96])
        self.assertFalse(bull_filter(rows)[32*96])
        self.assertTrue(bull_filter(rows)[33*96])
        self.assertEqual(filtered([1,-1,1],[False,False,True]),[0,-1,1])

    def test_signal_at_end_cannot_execute_within_period(self):
        start=int(dt.datetime(2025,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
        rows=[(start,100,100,100,100),(start+PERIOD,100,100,100,100)]
        got=diagnostic(rows,[0,1],'ETHUSDT',start,start+2*PERIOD)
        self.assertEqual(got['trade_count'],0)
        self.assertEqual(got['net'],0)

    def test_rejected_minimum_does_not_create_trade_or_cost(self):
        start=int(dt.datetime(2025,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
        rows=[(start,2000000,2000000,2000000,2000000),(start+PERIOD,2000000,2000000,2000000,2000000)]
        got=diagnostic(rows,[1,0],'BTCUSDT',start,start+2*PERIOD)
        self.assertEqual(got['entries_attempted'],1)
        self.assertEqual(got['rejected'],{'min_notional':1})
        self.assertEqual((got['trade_count'],got['costs'],got['net']),(0,0,0))

if __name__=='__main__':unittest.main()
