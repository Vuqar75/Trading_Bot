import unittest
from urllib.parse import parse_qs, urlparse

from trading_bot.cross_public_data import CrossPublicData
from trading_bot.testnet_observer import ObservationError


class CrossPublicDataTests(unittest.TestCase):
    def test_mark_uses_distinct_symbols_and_public_get(self):
        calls=[]
        def transport(req):
            parsed=urlparse(req.full_url);calls.append((req.get_method(),parsed.path))
            if parsed.path.endswith('/time'):return {'serverTime':100000}
            return {'symbol':parse_qs(parsed.query)['symbol'][0],'time':100000,'markPrice':'200'}
        reader=CrossPublicData(transport)
        for symbol in ('BTCUSDT','ETHUSDT'):self.assertEqual(str(reader.mark(symbol)[0]),'200')
        self.assertTrue(all(method=='GET' for method,path in calls))
        with self.assertRaises(ObservationError):reader.mark('XRPUSDT')

    def test_funding_rejects_wrong_symbol_and_disorder(self):
        def transport(req):
            return [{'symbol':'BTCUSDT','fundingTime':10,'fundingRate':'.001','markPrice':'100'},
                    {'symbol':'BTCUSDT','fundingTime':9,'fundingRate':'.001','markPrice':'100'}]
        reader=CrossPublicData(transport)
        with self.assertRaisesRegex(ObservationError,'identity/order'):reader.funding('ETHUSDT',0,100)
        with self.assertRaisesRegex(ObservationError,'identity/order'):reader.funding('BTCUSDT',0,100)

    def test_funding_history_is_not_coverage_proof(self):
        reader=CrossPublicData(lambda req:[])
        self.assertEqual(reader.funding('ETHUSDT',0,100),[])
        # A successful empty response supplies no coverage flag.
        self.assertIsInstance(reader.funding('BTCUSDT',0,100),list)


if __name__=='__main__':unittest.main()
