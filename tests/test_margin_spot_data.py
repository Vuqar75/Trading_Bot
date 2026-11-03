import json
import unittest
from decimal import Decimal
from urllib.parse import parse_qs,urlsplit

from trading_bot.margin_spot_data import MarginSpotData
from trading_bot.margin_spot_filters import affordable_lot,budget_lot,sellable_lot,validate_lot
from trading_bot.testnet_observer import ObservationError


def filters(step='0.00001',minimum='0.00001',notional='5'):
    return {'lot_rules':[{'min':minimum,'max':'100','step':step}],
            'min_notional':notional,'max_notional':'0','observed_ms':1000}


class SpotFilterTests(unittest.TestCase):
    def test_exact_steps_and_partial_dust(self):
        f=filters(step='.03',minimum='.03',notional='0')
        self.assertEqual(sellable_lot('.10','100',f),Decimal('.09'))
        self.assertEqual(affordable_lot('.10','12','100','0','100',f,allow_partial=False),Decimal('.12'))
        self.assertEqual(affordable_lot('.10','8','100','0','100',f,allow_partial=True),Decimal('.06'))
        with self.assertRaisesRegex(ValueError,'Insufficient'):
            affordable_lot('.10','8','100','0','100',f,allow_partial=False)
        with self.assertRaisesRegex(ValueError,'lot filters'):validate_lot('.10','100',f)

    def test_intersected_steps_and_tiny_residual(self):
        f={'lot_rules':[{'min':'0','max':'0','step':'.03'},{'min':'0','max':'0','step':'.02'}], 'min_notional':'0','max_notional':'0'}
        self.assertEqual(sellable_lot('.13','100',f),Decimal('.12'))
        self.assertEqual(sellable_lot('.01','100',f),Decimal(0))
        self.assertEqual(affordable_lot('.0000000000000000001','0','100','.001','100',f,allow_partial=True),Decimal(0))

    def test_notional_and_disabled_steps_are_conservative(self):
        self.assertEqual(sellable_lot('.04','100',filters(step='.01',minimum='.01',notional='5')),Decimal(0))
        with self.assertRaisesRegex(ValueError,'step unavailable'):
            sellable_lot('1','100',filters(step='0',minimum='0',notional='0'))

    def test_budget_lot_accounts_exactly_for_fees_and_increment(self):
        f=filters(step='.0001',minimum='.0001',notional='5')
        lot=budget_lot('10','100.02','.001','100',f)
        self.assertEqual(lot,Decimal('.0998'))
        self.assertLessEqual(lot*Decimal('100.02')*Decimal('1.001'),Decimal(10))
        self.assertGreater((lot+Decimal('.0001'))*Decimal('100.02')*Decimal('1.001'),Decimal(10))
        self.assertEqual(budget_lot('4','100','.001','100',f),Decimal(0))

    def test_exchange_reference_price_controls_notional(self):
        f=filters(step='.01',minimum='.01',notional='5');f['notional_price']='90'
        self.assertEqual(sellable_lot('.05','100',f),Decimal(0))
        self.assertEqual(sellable_lot('.06','100',f),Decimal('.06'))


class PublicSpotDataTests(unittest.TestCase):
    def test_public_allowlist_identity_and_closed_candles(self):
        period=900000;server=40*period+2000;requests=[]
        def transport(request):
            path=urlsplit(request.full_url).path;params=parse_qs(urlsplit(request.full_url).query);requests.append((request.get_method(),path))
            if path.endswith('/time'):return {'serverTime':server}
            symbol=params.get('symbol',['BTCUSDT'])[0]
            if path.endswith('/exchangeInfo'):
                return {'symbols':[{'symbol':symbol,'baseAsset':symbol[:-4],'quoteAsset':'USDT','status':'TRADING','orderTypes':['MARKET'],
                    'filters':[{'filterType':'LOT_SIZE','minQty':'.001','maxQty':'1000','stepSize':'.001'},
                               {'filterType':'MIN_NOTIONAL','minNotional':'5','avgPriceMins':5}]}]}
            if path.endswith('/trades'):return [{'id':123,'time':server-1000,'price':'100'}]
            if path.endswith('/referencePrice'):return {'symbol':symbol,'timestamp':server-100,'referencePrice':'99'}
            if path.endswith('/klines'):return [[i*period,0,0,0,'100',0,(i+1)*period-1] for i in range(40)]
            raise AssertionError(path)
        reader=MarginSpotData(transport=transport,clock=lambda:server/1000)
        result=reader.snapshot('BTCUSDT');self.assertEqual(result['source'],'https://api.binance.com')
        self.assertEqual(result['time_ms'],server-1000);self.assertEqual(result['price_kind'],'last_public_trade')
        self.assertEqual(result['candle_server_ms'],server)
        self.assertEqual(result['filters']['notional_price'],'99')
        self.assertEqual(len(result['candles']),40);self.assertEqual(result['filters']['min_notional'],'5')
        self.assertTrue(all(method=='GET' for method,_ in requests))
        with self.assertRaises(ObservationError):reader.get('/api/v3/order')
        with self.assertRaises(ObservationError):reader.snapshot('XRPUSDT')

    def test_candle_snapshot_crossing_boundary_uses_pre_fetch_cutoff(self):
        from trading_bot.cross_paper_strategy import CrossPaperStrategy
        period=900000;start=40*period-100;end=40*period+100;calls=0
        def transport(request):
            nonlocal calls
            path=urlsplit(request.full_url).path;params=parse_qs(urlsplit(request.full_url).query)
            symbol=params.get('symbol',['BTCUSDT'])[0]
            if path.endswith('/time'):
                calls+=1;return {'serverTime':start if calls==1 else end}
            if path.endswith('/exchangeInfo'):
                return {'symbols':[{'symbol':symbol,'baseAsset':symbol[:-4],'quoteAsset':'USDT','status':'TRADING','orderTypes':['MARKET'],
                  'filters':[{'filterType':'LOT_SIZE','minQty':'.001','maxQty':'1000','stepSize':'.001'},
                             {'filterType':'MIN_NOTIONAL','minNotional':'5','avgPriceMins':5}]}]}
            if path.endswith('/trades'):return [{'id':1,'time':start-100,'price':'100'}]
            if path.endswith('/referencePrice'):return {'symbol':symbol,'timestamp':start-100,'referencePrice':'100'}
            if path.endswith('/klines'):return [[i*period,0,0,0,'100',0,(i+1)*period-1] for i in range(40)]
            raise AssertionError(path)
        result=MarginSpotData(transport=transport,clock=lambda:end/1000).snapshot('BTCUSDT')
        self.assertEqual(result['candle_server_ms'],start)
        self.assertEqual(result['server_ms'],end)
        self.assertEqual(result['candles'][-1][0],38*period)
        CrossPaperStrategy._signal(result['candles'],'15m',result['candle_server_ms'])
        with self.assertRaisesRegex(ValueError,'Stale or future'):
            CrossPaperStrategy._signal(result['candles'],'15m',result['server_ms'])

    def test_stale_or_wrong_symbol_rejected(self):
        reader=MarginSpotData(transport=lambda req:{'serverTime':1},clock=lambda:1000)
        with self.assertRaisesRegex(ObservationError,'clock'):reader.snapshot('BTCUSDT')


if __name__=='__main__':unittest.main()
