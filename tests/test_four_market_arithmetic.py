"""Independent closed-form isolated futures examples using synthetic prices."""
import tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_store import FuturesStore

class IsolatedArithmetic(unittest.TestCase):
 def test_btc_and_sol_long_short_fees_and_funding(self):
  for symbol in ('BTCUSDT','SOLUSDT'):
   for direction,exit_price,expected in (('LONG','110','0.9795'),('SHORT','90','1.0005')):
    with self.subTest(symbol=symbol,direction=direction), tempfile.TemporaryDirectory() as tmp:
     s=FuturesStore(Path(tmp)/'futures.db',symbol=symbol,allow_pair_experiment=True,slippage_bps='0')
     try:
      metadata=spot_info(symbol[:-4]);metadata.update(contractType='PERPETUAL',marginAsset='USDT')
      s.command(1,True,'/resume')
      self.assertTrue(s.open_position(direction,Decimal('100'),1000,'entry',metadata=metadata,quote_symbol=symbol,observed_at=time.time()))
      p=s.get('position');self.assertEqual(Decimal(p['quantity']),Decimal('.1'));self.assertEqual(Decimal(s.get('cash')),Decimal('990'))
      s.apply_funding(p['id'],[{'symbol':symbol,'time':2000,'rate':'.001','mark':'100'}],3000)
      self.assertTrue(s.close_position(Decimal(exit_price),3000,'exit','fixture',p['id'],quote_symbol=symbol,observed_at=time.time()))
      # Gross 1; entry fee .005; exit fee .0055/.0045; funding -.01/+.01.
      self.assertEqual(Decimal(s.get('pnl')),Decimal(expected))
      self.assertEqual(Decimal(s.get('cash')),Decimal('1000')+Decimal(expected))
      self.assertFalse(s.close_position(Decimal(exit_price),3000,'exit','fixture',p['id'],quote_symbol=symbol,observed_at=time.time()))
     finally:s.close()
