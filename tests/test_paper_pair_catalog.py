import copy
import unittest
from decimal import Decimal

from trading_bot.paper_pair_catalog import PairMetadataError, spot_usdt_candidate
from trading_bot.runtime_exchange import order_quantity


def spot_info(base='SOL'):
    return {'symbol': base+'USDT', 'baseAsset': base, 'quoteAsset': 'USDT',
            'status': 'TRADING', 'isSpotTradingAllowed': True, 'orderTypes': ['MARKET'],
            'filters': [{'filterType': 'LOT_SIZE', 'minQty': '0.001', 'maxQty': '1000', 'stepSize': '0.001'},
                        {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0', 'maxQty': '500', 'stepSize': '0'},
                        {'filterType': 'MIN_NOTIONAL', 'minNotional': '5'}]}


class PaperPairCatalogTests(unittest.TestCase):
    def test_third_pair_preview_uses_its_own_asset_filters_and_decimal(self):
        info=spot_info();before=copy.deepcopy(info)
        value=spot_usdt_candidate(info,'SOLUSDT','0.123456789','200')
        self.assertEqual(value,{'symbol':'SOLUSDT','base_asset':'SOL','quote_asset':'USDT',
                                'quantity':'0.123','reference_notional':'24.600'})
        self.assertEqual(info,before)
        self.assertEqual(spot_usdt_candidate(info,'SOLUSDT','0.123456789','200'),value)

    def test_existing_btc_filter_result_agrees_with_old_adapter(self):
        info=spot_info('BTC')
        self.assertEqual(spot_usdt_candidate(info,'BTCUSDT','0.12345','200')['quantity'],
                         order_quantity(info,Decimal('0.12345'),Decimal('200')))

    def test_reject_mismatched_non_usdt_inactive_or_unsupported_market(self):
        for mutate in ({'symbol':'BTCUSDT'}, {'quoteAsset':'BTC'}, {'status':'BREAK'},
                       {'isSpotTradingAllowed':False}, {'orderTypes':['LIMIT']}):
            with self.subTest(mutate=mutate):
                info=spot_info();info.update(mutate)
                with self.assertRaises(PairMetadataError):spot_usdt_candidate(info,'SOLUSDT','1','200')

    def test_reject_missing_or_duplicate_rules(self):
        for filters in ([],[{'filterType':'LOT_SIZE'}],spot_info()['filters']+[spot_info()['filters'][0]]):
            with self.subTest(filters=filters):
                info=spot_info();info['filters']=filters
                with self.assertRaises(PairMetadataError):spot_usdt_candidate(info,'SOLUSDT','1','200')

    def test_reject_lot_notional_and_malformed_values(self):
        info=spot_info()
        for qty,price in (('0.0001','200'),('0.01','200'),('501','200'),('1','NaN'),('-1','200'),('1','0'),('1e-1000000','200')):
            with self.subTest(qty=qty,price=price):
                with self.assertRaises(PairMetadataError):spot_usdt_candidate(info,'SOLUSDT',qty,price)

    def test_both_lot_steps_and_notional_ceiling_are_enforced(self):
        info=spot_info();info['filters'][1]={'filterType':'MARKET_LOT_SIZE','minQty':'0.01','maxQty':'100','stepSize':'0.01'}
        info['filters'].append({'filterType':'NOTIONAL','minNotional':'5','maxNotional':'30'})
        self.assertEqual(spot_usdt_candidate(info,'SOLUSDT','0.129','200')['quantity'],'0.12')
        with self.assertRaises(PairMetadataError):spot_usdt_candidate(info,'SOLUSDT','0.20','200')


if __name__ == '__main__':
    unittest.main()
