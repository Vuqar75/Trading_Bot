import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from trading_bot.paper_wallet_ledger import PaperWalletLedger
from trading_bot.paper_wallet_report import balance_report,render_balance_report


class WalletReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=PaperWalletLedger(Path(self.tmp.name)/'journal.db');self.addCleanup(self.ledger.close)
        self.pos={'spot':{'id':'s','quantity':'0.1','cost':'10','fees_known':True},
                  'futures':{'id':'f','direction':'SHORT','quantity':'0.1','entry':'100','margin':'10','entry_fee':'0.005','funding':'0.002'},
                  'spot_realized_pnl':'-1','futures_realized_pnl':'2',
                  'futures_identity':{'fee_bps':'5','slippage_bps':'2'}}
        self.ledger.import_snapshot(spot_cash='90',spot_btc='.1',futures_cash='90',futures_margin='10',funding='.002',position_metadata=self.pos,source='fixture',captured_at=100)
    def test_equity_no_double_counting_and_transfer_conservation(self):
        kwargs={'spot_quote':{'price':'100','time':100},'futures_quote':{'price':'100','time':100},'now':101}
        before=balance_report(self.ledger,**kwargs)
        self.assertEqual(Decimal(before['estimated_snapshot_equity']),Decimal('199.9900000')-Decimal('0.000001'))
        p=self.ledger.propose_transfer('x','spot','futures_isolated','20',now=100)
        self.ledger.confirm_transfer(p['id'],now=101)
        after=balance_report(self.ledger,**kwargs)
        self.assertEqual(after['estimated_snapshot_equity'],before['estimated_snapshot_equity'])
        self.assertEqual(after['spot_realized_pnl'],'-1')
        self.assertEqual(after['futures_realized_pnl'],'2')
        self.assertFalse(after['complete'])
        self.assertIn('снимок',render_balance_report(after))
    def test_missing_or_stale_quote_is_unknown_not_zero(self):
        data=balance_report(self.ledger,spot_quote={'price':'100','time':1},futures_quote={'price':'100','time':1},now=500)
        self.assertIsNone(data['spot_value_usdt'])
        self.assertIsNone(data['estimated_snapshot_equity'])
        self.assertIsNone(data['historical_fees'])
        self.assertTrue(any('свежей' in x for x in data['issues']))
    def test_unknown_spot_fee_keeps_total_unknown(self):
        other=PaperWalletLedger(Path(self.tmp.name)/'unknown-fee.db');self.addCleanup(other.close)
        positions={**self.pos,'spot':{**self.pos['spot'],'fees_known':False}}
        other.import_snapshot(spot_cash='90',spot_btc='.1',futures_cash='90',futures_margin='10',funding='.002',position_metadata=positions,source='fixture',captured_at=100)
        data=balance_report(other,spot_quote={'price':'100','time':100},futures_quote={'price':'100','time':100},now=101)
        self.assertIsNone(data['estimated_snapshot_equity'])
        self.assertTrue(any('комисси' in x for x in data['issues']))
    def test_today_uses_baku_calendar_boundary(self):
        day=datetime(2026,11,1,0,0,tzinfo=ZoneInfo('Asia/Baku')).timestamp()
        old=self.ledger.propose_transfer('old','spot','reserve','1',now=day-3)
        self.ledger.confirm_transfer(old['id'],now=day-2)
        new=self.ledger.propose_transfer('new','spot','reserve','1',now=day+1)
        self.ledger.confirm_transfer(new['id'],now=day+2)
        data=balance_report(self.ledger,spot_quote={'price':'100','time':day},futures_quote={'price':'100','time':day},now=day+3,period='today')
        self.assertEqual(len(data['internal_transfers']),1)
        self.assertIsNone(data['spot_realized_pnl'])
        self.assertIsNone(data['futures_realized_pnl'])
