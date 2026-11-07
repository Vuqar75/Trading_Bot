import unittest
from trading_bot.spot_pilot_rules import BAR,evaluate

class PilotRulesTest(unittest.TestCase):
 def rows(self):
  rows=[]
  for i in range(201):rows.append([i*BAR,'100','101','99','100','1',(i+1)*BAR-1])
  return rows
 def test_entry_excludes_current_hour_and_waits_one_bar(self):
  rows=self.rows();rows[199][2]='103';rows[199][4]='102'
  out=evaluate(rows,server_ms=201*BAR,start_ms=1,position_open=False)
  self.assertEqual((out['action'],out['entry_high'],out['close']),('BUY','100','102'))
  self.assertEqual(out['execution_boundary_ms'],201*BAR)
 def test_exit_uses_previous_24_hour_closes(self):
  rows=self.rows();rows[199][3]='97';rows[199][4]='98'
  self.assertEqual(evaluate(rows,server_ms=201*BAR,start_ms=1,position_open=True)['action'],'SELL')
 def test_prestart_signal_and_gap_are_rejected(self):
  rows=self.rows();rows[199][2]='103';rows[199][4]='102'
  self.assertEqual(evaluate(rows,server_ms=201*BAR,start_ms=200*BAR,position_open=False)['reason'],'before_start')
  del rows[50]
  with self.assertRaisesRegex(ValueError,'discontinuous'):evaluate(rows,server_ms=201*BAR,start_ms=1,position_open=False)
 def test_unclosed_skip_cannot_execute(self):
  with self.assertRaises(ValueError):evaluate(self.rows(),server_ms=201*BAR-1,start_ms=1,position_open=False)
 def test_matches_saved_research_rule(self):
  from research.adaptive_study import signals
  rows=self.rows();rows[199][2]='103';rows[199][4]='102'
  source=[(r[0],*[float(x) for x in r[1:5]]) for r in rows]
  research=signals(source,'trend1')[199]
  result=evaluate(rows,server_ms=201*BAR,start_ms=1,position_open=False)
  self.assertEqual((float(result['close']),float(result['entry_high']),float(result['exit_low'])),(research[1],research[2],research[4]))
