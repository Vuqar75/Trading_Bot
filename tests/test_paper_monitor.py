import tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.app import Application
from trading_bot.paper_monitor import snapshot
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore

class PaperMonitorTests(unittest.TestCase):
 def test_fees_realized_and_sampled_drawdown(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'runtime.db';o=Path(d)/'observation.db';s=RuntimeStore(p,{'mode':'paper'});a=Application(s,PaperExchange(),None)
   a.enable_paper_entries();a.execute('BUY','0.1',Decimal('100'),'buy','TEST');pos=s.get('position')
   with s.transaction():s.set('quote',{'price':'100','time':1000})
   first=snapshot(p,o,1000);self.assertEqual(first['total_fees_usdt'],'0.0100')
   with s.transaction():s.set('quote',{'price':'90','time':1060})
   second=snapshot(p,o,1060);self.assertEqual(Decimal(second['max_sampled_drawdown_usdt']),Decimal('1.0'))
   a.execute('SELL','0.1',Decimal('102'),'sell','TEST',pos['id'])
   with s.transaction():s.set('quote',{'price':'102','time':1120})
   last=snapshot(p,o,1120)
   self.assertEqual(Decimal(last['net_realized_pnl_usdt']),Decimal('.1798'))
   self.assertEqual(Decimal(last['gross_realized_pnl_usdt']),Decimal('.2'))
   self.assertEqual(last['closed_positions'],1);self.assertEqual(last['pending_orders'],0);s.close()
 def test_refuses_testnet_and_records_gaps(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'runtime.db';o=Path(d)/'obs.db';s=RuntimeStore(p,{'mode':'paper'})
   snapshot(p,o,1000);report=snapshot(p,o,1200)
   self.assertEqual(report['sample_gaps_over_120s'],1);self.assertEqual(report['stale_or_missing_quotes'],2);s.close()
