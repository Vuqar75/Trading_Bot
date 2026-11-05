import copy
import unittest
from decimal import Decimal, localcontext

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

    def test_asset_and_numeric_precision_do_not_exceed_current_ledger_limits(self):
        info=spot_info('ABCDEFGHIJKLM')
        with self.assertRaisesRegex(PairMetadataError,'unsupported_symbol'):
            spot_usdt_candidate(info,'ABCDEFGHIJKLMUSDT','1','200')
        info=spot_info()
        for qty in ('1e20','1.'+'0'*81):
            with self.subTest(qty=qty):
                with self.assertRaisesRegex(PairMetadataError,'invalid_numeric_filter'):
                    spot_usdt_candidate(info,'SOLUSDT',qty,'200')

    def test_computed_repeating_decimal_keeps_precision_but_is_bounded(self):
        from trading_bot.paper_pair_catalog import _preview_quantity
        info=spot_info('BTC');info['filters'][0].update({'minQty':'0.00001000','stepSize':'0.00001000','maxQty':'9000'})
        with localcontext() as ctx:
            ctx.prec=120;ratio=Decimal(10)/Decimal('80506.01000000')
        self.assertEqual(_preview_quantity(ratio),ratio)
        preview=spot_usdt_candidate(info,'BTCUSDT',ratio,'80506.01000000')
        self.assertEqual(preview['quantity'],'0.00012000')
        self.assertLessEqual(Decimal(preview['reference_notional']),Decimal(10))
        for bad in (Decimal('1.'+'1'*120),Decimal('1e-181'),Decimal('1e20')):
            with self.assertRaises(PairMetadataError):_preview_quantity(bad)


if __name__ == '__main__':
    unittest.main()
