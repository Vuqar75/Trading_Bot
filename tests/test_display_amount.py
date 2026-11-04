import unittest
from decimal import Decimal
from trading_bot.display_amount import display_amount
from trading_bot.trade_notifications import fees_text, spot_receipt, futures_receipt


class DisplayAmountTests(unittest.TestCase):
    def test_units_thresholds_rounding_and_sign(self):
        for unit in ('USD','USDT','USDC'):
            self.assertEqual(display_amount('77000.123456',unit),'77000.1235')
            self.assertEqual(display_amount('0.00001',unit),'меньше 0.0001')
            self.assertEqual(display_amount('-0.00001',unit),'-меньше 0.0001')
        for unit in ('BTC','ETH','BNB'):
            self.assertEqual(display_amount('0.0001200000123',unit),'0.00012')
            self.assertEqual(display_amount('0.00000000001',unit),'меньше 0.0000000001')
            self.assertEqual(display_amount('-0.00000000001',unit),'-меньше 0.0000000001')
            self.assertEqual(display_amount('0.00000000016',unit),'0.0000000002')
        self.assertEqual(display_amount(Decimal('-0'),'BTC'),'0')
        self.assertEqual(display_amount(None,'BTC'),'неизвестно')
        with self.assertRaises(TypeError):display_amount(0.1,'BTC')

    def test_receipt_uses_fee_asset_and_does_not_hide_tiny_crypto(self):
        text=fees_text({'BTC':'0.00000000001','USDT':'0.00001','ETH':'0.000000012345'})
        self.assertIn('меньше 0.0000000001 BTC',text)
        self.assertIn('меньше 0.0001 USDT',text)
        self.assertIn('0.0000000123 ETH',text)
        receipt=spot_receipt(mode='paper',side='BUY',status='FILLED',quantity='0.00000000001',average='77000.123456',fees={'BTC':'0.00000000001'},remaining='0.00000000001',reason='TEST',interval='15m',client='fixture')
        self.assertIn('меньше 0.0000000001 BTC',receipt)
        self.assertIn('77000.1235 USDT',receipt)

    def test_futures_receipt_formats_position_and_small_funding(self):
        data={'direction':'SHORT','leverage':1,'opened_ms':0,'margin':'5','quantity':'0.0001200000123','entry':'77000.123456','entry_fee':'0.00001'}
        text=futures_receipt('OPEN',data,interval='15m')
        self.assertIn('0.00012 BTC',text);self.assertIn('77000.1235 USDT',text)
        self.assertIn('меньше 0.0001 USDT',text)
