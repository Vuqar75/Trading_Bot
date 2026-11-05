import unittest
from decimal import Decimal
from trading_bot import spot_donchian as d

class DonchianTests(unittest.TestCase):
 def test_signal_excludes_current_day_and_is_strict(self):
  self.assertEqual(d.decide(['100']*60+['101'],position_open=False)[0],'BUY')
  self.assertEqual(d.decide(['100']*61,position_open=False)[0],'HOLD')
 def test_exit_before_raise_and_never_falls(self):
  values=['100']*60+['89'];self.assertEqual(d.decide(values,position_open=True,prior_level='90'),('SELL',Decimal(90)))
  self.assertEqual(d.decide(['100']*61,position_open=True,prior_level='120'),('SELL',Decimal(120)))
  self.assertEqual(d.decide(['80']*60+['100'],position_open=True,prior_level='95'),('HOLD',Decimal(95)))
 def test_closed_days_boundary_warmup_gap_duplicate(self):
  server=100*d.DAY_MS;rows=[[i*d.DAY_MS,'1','1','1','1','0',(i+1)*d.DAY_MS-1] for i in range(39,101)]
  accepted=d.closed_days(rows,server);self.assertEqual(len(accepted),61);self.assertEqual(accepted[-1][0],99*d.DAY_MS)
  with self.assertRaises(ValueError):d.closed_days(rows[:20]+rows[21:],server)
  with self.assertRaises(ValueError):d.closed_days(rows[:-1]+[rows[-2]],server)
 def test_execution_requires_signal_and_new_trade_and_window(self):
  b=100*d.DAY_MS;ok=dict(boundary_ms=b,signal_commit_ms=b+10,request_ms=b+20,trade_ms=b+11,received_ms=b+30,server_before_ms=b+20,server_after_ms=b+30)
  d.validate_execution(**ok)
  for changes in ({'trade_ms':b+10},{'request_ms':b+10},{'received_ms':b+d.WINDOW_MS+1},{'trade_ms':b+1},{'server_after_ms':b+7000}):
   with self.assertRaises(ValueError):d.validate_execution(**(ok|changes))
