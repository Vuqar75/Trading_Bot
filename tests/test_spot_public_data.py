import unittest
from trading_bot.spot_public_data import SpotPublicData
class PublicReaderTest(unittest.TestCase):
 def test_only_public_get_and_no_secrets(self):
  seen=[];r=SpotPublicData(transport=lambda req:seen.append(req) or {'serverTime':1});r.get('/api/v3/time')
  self.assertTrue(seen[0].full_url.startswith('https://data-api.binance.vision/'));self.assertEqual(seen[0].get_method(),'GET');self.assertFalse(seen[0].headers)
  for path,signed in (('/api/v3/order',False),('/api/v3/account',False),('/api/v3/time',True)):
   with self.assertRaises(Exception):r.get(path,signed=signed)
