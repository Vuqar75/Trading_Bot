import unittest
from trading_bot.spot_check_guard import validate

class CheckGuardTest(unittest.TestCase):
 def state(self):return {'schema':1,'authorization':'owner_local_once','id':'case1','phase':'BUY_ARMED','quote_event_ms':100100,'quote_received_ms':100200}
 def test_buy_is_bounded_and_fresh(self):
  s=self.state();args={'side':'BUY','business_key':'paper-check:case1:buy','quantity':'.1','price':'100','quote_at':100.2,'now_ms':100300}
  self.assertTrue(validate(s,**args))
  with self.assertRaisesRegex(ValueError,'notional'):validate(s,**{**args,'quantity':'.101'})
  with self.assertRaisesRegex(ValueError,'stale'):validate(s,**{**args,'now_ms':106000})
 def test_identity_and_owned_sell_are_rechecked(self):
  s=self.state();s.update(phase='SELL_ARMED',quantity='0.1')
  args={'side':'SELL','business_key':'paper-check:case1:sell','quantity':'0.10','price':'99','quote_at':100.2,'now_ms':100300}
  self.assertTrue(validate(s,**args))
  with self.assertRaisesRegex(ValueError,'quantity'):validate(s,**{**args,'quantity':'.2'})
  with self.assertRaisesRegex(ValueError,'phase'):validate(s,**{**args,'business_key':'other'})
