import unittest
from research.donchian_data import DAY,aggregate,ms
from research.donchian60 import Book,run_daily,summarise

class DonchianTests(unittest.TestCase):
    def candles(self,prices,start=ms(2024,1,1)-60*DAY):
        return [(start+i*DAY,p,p,p,p) for i,p in enumerate(prices)]
    def test_current_day_excluded_and_entry_next_open(self):
        rows=self.candles([100.]*60+[110.,111.]);start=rows[60][0]
        b=run_daily(rows,'ETHUSDT',start,rows[-1][0]+DAY)
        self.assertEqual(len(b.events),1);self.assertEqual(b.events[0]['t'],rows[61][0]);self.assertEqual(b.events[0]['type'],'BUY')
        self.assertAlmostEqual(b.pos['ref'],111.)
        tied=self.candles([100.]*63);self.assertEqual(run_daily(tied,'ETHUSDT',tied[60][0],tied[-1][0]+DAY).events,[])
    def test_exit_checks_previous_level_and_does_not_decrease(self):
        rows=self.candles([100.]*60+[110.,105.,104.,90.]);b=run_daily(rows,'ETHUSDT',rows[60][0],rows[-1][0]+DAY)
        self.assertEqual(len(b.trades),1);self.assertEqual(b.trades[0]['exit_ms'],rows[-1][0]);self.assertEqual(b.trades[0]['exit_ref'],90.)
        self.assertTrue(all(after>=before for _,before,after,_ in b.levels));self.assertEqual(b.levels[-1][-1],-1)
    def test_costs_and_rounding(self):
        b=Book('ETHUSDT',5);self.assertTrue(b.buy(ms(2024),100,90));b.sell(ms(2024,1,2),100)
        q=.0999;expected=q*100*.9995*.999-q*100*1.0005*1.001
        self.assertAlmostEqual(b.trades[0]['net'],expected,places=10)
    def test_minimum_filter_and_final_signal(self):
        b=Book('BTCUSDT',5);self.assertFalse(b.buy(ms(2024),2000000,90));self.assertEqual(b.rejected,1);self.assertEqual(b.events,[])
        rows=self.candles([100.]*60+[110.]);end=rows[-1][0]+DAY
        b=run_daily(rows,'ETHUSDT',rows[60][0],end)
        self.assertEqual(b.events,[]);self.assertEqual(b.pending,1)
    def test_gap_in_daily_aggregation_fails(self):
        rows=[(ms(2024)+i*900000,1,1,1,1) for i in range(96)];self.assertEqual(len(aggregate(rows)),1)
        rows[40]=(rows[40][0]+900000,1,1,1,1)
        with self.assertRaises(ValueError):aggregate(rows)
        with self.assertRaises(ValueError):aggregate(rows[:-1])
    def test_year_boundary_preserves_position_and_equity(self):
        start=ms(2024,12,30)-60*DAY;rows=self.candles([100.]*60+[110.,111.,104.,90.],start)
        b=run_daily(rows,'ETHUSDT',rows[60][0],rows[-1][0]+DAY)
        self.assertEqual(b.events[0]['t'],ms(2024,12,31));self.assertEqual(b.trades[0]['exit_ms'],ms(2025,1,2))
        a=summarise(b,ms(2024,12,30),ms(2025));c=summarise(b,ms(2025),ms(2025,1,3));whole=summarise(b,ms(2024,12,30),ms(2025,1,3))
        self.assertTrue(a['open_end']);self.assertFalse(c['open_end'])
        self.assertAlmostEqual(a['net']+c['net'],whole['net'],places=9)
    def test_future_cannot_change_prior_entries(self):
        rows=self.candles([100.]*60+[110.,111.,112.,113.]);cut=rows[62][0];end=rows[-1][0]+DAY
        a=run_daily(rows,'ETHUSDT',rows[60][0],end);altered=rows[:62]+self.candles([900.,900.],cut);b=run_daily(altered,'ETHUSDT',rows[60][0],end)
        self.assertEqual([e for e in a.events if e['t']<cut],[e for e in b.events if e['t']<cut])
        self.assertEqual([e for e in a.levels if e[0]<cut],[e for e in b.levels if e[0]<cut])

if __name__=='__main__':unittest.main()
