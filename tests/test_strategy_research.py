import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research.strategy_study import PERIOD, download_one, signals, simulate
from trading_bot.core import signal_details


class StrategyStudyTests(unittest.TestCase):
    def setUp(self):
        self.start=int(dt.datetime(2025,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)

    def run_case(self, rows, sig, **kwargs):
        return simulate(rows,sig,['up']*len(rows),'ETHUSDT',2025,**kwargs)

    def test_next_open_and_independent_round_trip_arithmetic(self):
        rows=[(self.start,100,100,100,100),(self.start+PERIOD,110,110,110,110)]
        got=self.run_case(rows,[1,0],cost_bps=5)
        q=0.0908;buy=110*1.0005;sell=110*.9995
        expected=q*sell*.999-q*buy*1.001
        self.assertAlmostEqual(got['net'],expected,places=9)
        self.assertEqual(got['trades'],1)

    def test_simultaneous_stop_take_is_stop_first(self):
        rows=[(self.start,100,100,100,100),(self.start+PERIOD,100,110,90,100)]
        low=self.run_case(rows,[1,0],cost_bps=0)
        high=self.run_case(rows,[1,0],cost_bps=0,take_first=True)
        self.assertEqual(low['ambiguous'],1)
        self.assertAlmostEqual(high['net']-low['net'],.6*.999,places=9)

    def test_stop_gap_uses_open(self):
        rows=[(self.start,100,100,100,100),(self.start+PERIOD,100,100,100,100),(self.start+2*PERIOD,90,91,89,90)]
        got=self.run_case(rows,[1,0,0],cost_bps=0)
        self.assertAlmostEqual(got['net'],.1*90*.999-10*1.001,places=9)

    def test_future_prices_cannot_change_prior_signals(self):
        rows=[(self.start+i*PERIOD,100+i%7,101+i%7,99+i%7,100+i%7) for i in range(250)]
        modified=rows[:220]+[(t,o,h*2,l,c*1.5) for t,o,h,l,c in rows[220:]]
        first,_=signals(rows);second,_=signals(modified)
        for name in first:self.assertEqual(first[name][:220],second[name][:220])

    def test_current_signal_agrees_with_application(self):
        rows=[(self.start+i*PERIOD,100+i%11,101+i%11,99+i%11,100+i%11) for i in range(400)]
        calculated,_=signals(rows)
        for i in range(30,len(rows)):
            action=signal_details([r[4] for r in rows[:i+1]])['action']
            self.assertEqual(calculated['current'][i],{'BUY':1,'SELL':-1,'HOLD':0}[action])

    def test_no_signal_means_no_trade(self):
        rows=[(self.start,100,100,100,100),(self.start+PERIOD,100,100,100,100)]
        got=self.run_case(rows,[0,0]);self.assertEqual(got['trades'],0);self.assertEqual(got['net'],0)

    def test_modified_archive_checksum_is_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('research.strategy_study.urllib.request.urlopen',return_value=io.BytesIO((('a'*64)+'  archive.zip').encode())) as get:
                with self.assertRaisesRegex(ValueError,'Archive version changed'):
                    download_one(('BTCUSDT',2025,1,Path(folder),'b'*64))
                self.assertEqual(get.call_count,1)
                self.assertEqual(list(Path(folder).iterdir()),[])

if __name__=='__main__':unittest.main()
