import unittest
from decimal import Decimal
from research.usdm_risk import funding,funding_belongs,liquidation
class UsdmRiskTests(unittest.TestCase):
 def test_signed_funding_notional(self):
  self.assertEqual(funding('LONG','.01','100000','.0001'),Decimal('-.1000000'))
  self.assertEqual(funding('SHORT','.01','100000','.0001'),Decimal('.1000000'))
  self.assertEqual(funding('SHORT','.01','100000','-.0001'),Decimal('-.1000000'))
 def test_ownership_boundary(self):
  self.assertFalse(funding_belongs(9,10,20));self.assertTrue(funding_belongs(10,10,20));self.assertTrue(funding_belongs(19,10,20));self.assertFalse(funding_belongs(20,10,20))
 def test_isolated_mark_and_leverage(self):
  self.assertEqual(liquidation('LONG',1,100,90,2)['initial_margin'],Decimal('50'))
  self.assertFalse(liquidation('LONG',1,100,51,2)['liquidated'])
  self.assertTrue(liquidation('LONG',1,100,50,2)['liquidated'])
  self.assertTrue(liquidation('SHORT',1,100,150,2)['liquidated'])
  self.assertFalse(liquidation('SHORT',1,100,110,1)['liquidated'])
 def test_commission_from_full_notional(self):
  x=liquidation('LONG','.1',100,100,2)
  self.assertEqual(x['initial_margin'],Decimal('5'))
  self.assertEqual(x['isolated_equity'],Decimal('4.99500'))
if __name__=='__main__':unittest.main()
