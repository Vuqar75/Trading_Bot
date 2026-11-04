"""Synthetic USD-M symbol and filter checks; never calls a network endpoint."""
import copy
import unittest
from urllib.parse import parse_qs, urlparse

from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_data import FuturesData
from trading_bot.paper_pair_catalog import PairMetadataError, usd_m_perpetual_candidate
from trading_bot.testnet_observer import ObservationError


class PairFuturesDataTests(unittest.TestCase):
    def metadata(self):
        data=spot_info();data.update(contractType='PERPETUAL',marginAsset='USDT')
        data['filters'][-1]={'filterType':'MIN_NOTIONAL','notional':'5'}
        return data

    def test_contract_preview_rejects_non_perpetual_or_wrong_margin(self):
        data=self.metadata();original=copy.deepcopy(data)
        self.assertEqual(usd_m_perpetual_candidate(data,'SOLUSDT','.1234','100')['quantity'],'0.123')
        self.assertEqual(data,original)
        for change in ({'contractType':'CURRENT_QUARTER'},{'marginAsset':'BTC'},{'status':'BREAK'}):
            bad=copy.deepcopy(data);bad.update(change)
            with self.subTest(change=change),self.assertRaises(PairMetadataError):
                usd_m_perpetual_candidate(bad,'SOLUSDT','.1','100')

    def test_futures_reader_binds_mark_candles_and_funding_to_symbol(self):
        calls=[];server=40*900000
        def transport(request):
            url=urlparse(request.full_url);params=parse_qs(url.query);calls.append((url.path,params))
            if url.path.endswith('/time'):return {'serverTime':server}
            if url.path.endswith('/premiumIndex'):return {'symbol':'SOLUSDT','time':server,'markPrice':'100'}
            if url.path.endswith('/klines'):
                return [[n*900000,0,0,0,'100',0,(n+1)*900000-1] for n in range(40)]
            if url.path.endswith('/fundingRate'):
                return [{'symbol':'SOLUSDT','fundingTime':1000,'fundingRate':'0.001','markPrice':'100'}]
            raise AssertionError(url.path)
        reader=FuturesData(transport,symbol='SOLUSDT')
        mark,stamp=reader.mark();self.assertEqual(str(mark),'100')
        self.assertEqual(len(reader.candles('15m',stamp)),40)
        self.assertEqual(reader.funding(0,2000)[0]['time'],1000)
        self.assertTrue(all(params.get('symbol')==['SOLUSDT'] for path,params in calls if not path.endswith('/time')))

    def test_wrong_symbol_and_invalid_identifier_fail_closed(self):
        for symbol in ('../SOLUSDT','USDTUSDT','solusdt','BTCBUSD'):
            with self.subTest(symbol=symbol),self.assertRaises(ObservationError):FuturesData(symbol=symbol)
        reader=FuturesData(lambda request: {'serverTime':1000} if request.full_url.endswith('/time') else
                           {'symbol':'BTCUSDT','time':1000,'markPrice':'100'},symbol='SOLUSDT')
        with self.assertRaisesRegex(ObservationError,'identity'):reader.mark()
        wrong=FuturesData(lambda request:[{'symbol':'BTCUSDT','fundingTime':1000,'fundingRate':'0.001','markPrice':'100'}],symbol='SOLUSDT')
        with self.assertRaisesRegex(ObservationError,'identity'):wrong.funding(0,2000)
