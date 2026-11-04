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

    def test_funding_boundary_keeps_same_time_distinct_rate_types(self):
        def row(stamp,kind='Regular'):
            return {'symbol':'BTCUSDT','fundingTime':stamp,'fundingRate':'.001','markPrice':'100','rateType':kind}
        page=[row(i) for i in range(999)]+[row(998,'Special')]
        def transport(req):
            cursor=int(parse_qs(urlparse(req.full_url).query)['startTime'][0])
            return page if cursor==0 else [row(998),row(998,'Special'),row(999)]
        events=CrossPublicData(transport).funding('BTCUSDT',0,1000)
        self.assertEqual(len(events),1001)
        self.assertEqual({x['rate_type'] for x in events if x['time']==998},{'Regular','Special'})

    def test_schedule_uses_adjustments_or_documented_default(self):
        def transport(req):
            parsed=urlparse(req.full_url)
            if parsed.path.endswith('/time'):return {'serverTime':100000}
            if parsed.path.endswith('/fundingInfo'):return [{'symbol':'ETHUSDT','fundingIntervalHours':4}]
            return {'symbol':parse_qs(parsed.query)['symbol'][0],'time':100000,'nextFundingTime':100001,'markPrice':'100'}
        reader=CrossPublicData(transport)
        self.assertEqual(reader.schedule('BTCUSDT')['interval_ms'],8*3600000)
        self.assertEqual(reader.schedule('ETHUSDT')['interval_ms'],4*3600000)


if __name__=='__main__':unittest.main()
