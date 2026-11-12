import unittest
from unittest.mock import patch

import numpy as np

from research import taker_absorption as study


class AbsorptionTests(unittest.TestCase):
    def fixture(self, long=False):
        bars = np.tile([100., 100.5, 99.5, 100.], (120, 1))
        bars[106] = [100., 102., 99.5, 101.5] if long else [100., 100.5, 98., 98.5]
        trade = np.repeat(bars, 15, axis=0)
        volume = np.ones(len(trade)); buy = volume*.5
        volume[105*15:106*15] = 3
        buy[105*15:106*15] = 3*(.2 if long else .8)
        return trade, volume, buy

    def test_short_and_long_require_next_closed_confirmation(self):
        for long in (False, True):
            trade, volume, buy = self.fixture(long)
            out, setups = study.signals(trade, volume, buy, 0)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]['side'], 1 if long else -1)
            self.assertEqual(out[0]['setup_ms'], 106*study.BAR)
            self.assertEqual(out[0]['T'], 107*study.BAR)
            self.assertEqual(out[0]['previous_volume_mean'], 15)
            self.assertEqual(out[0]['prior_atr'], 1)
            self.assertAlmostEqual(out[0]['imbalance'], -.6 if long else .6)
            self.assertEqual(study.signals(trade[:106*15], volume[:106*15], buy[:106*15], 0)[0], [])
            self.assertEqual(study.signals(trade[:107*15], volume[:107*15], buy[:107*15], 0)[0], out)

    def test_touch_without_close_is_not_confirmation(self):
        trade, volume, buy = self.fixture()
        trade[106*15:107*15, 3] = 100
        out, setups = study.signals(trade, volume, buy, 0)
        self.assertEqual(out, [])
        self.assertFalse(setups[0]['confirmed'])

    def test_future_price_and_volume_cannot_change_formed_signal(self):
        trade, volume, buy = self.fixture()
        first = study.signals(trade, volume, buy, 0)[0][0]
        trade[107*15:] *= 2
        volume[107*15:] = 1000
        buy[107*15:] = 800
        self.assertEqual(study.signals(trade, volume, buy, 0)[0][0], first)

    def test_missing_or_inconsistent_buy_volume_rejected(self):
        for invalid in (float('nan'), -1., 4.):
            trade, volume, buy = self.fixture()
            buy[100] = invalid
            with self.assertRaises(ValueError):
                study.signals(trade, volume, buy, 0)

    def test_separate_100_notional_cap_cannot_inherit_larger_btc_limit(self):
        n = 300; trade = np.tile([120000., 120001., 119999., 120000.], (n, 1))
        proposed = {'net': 0, 'qty': .001, 'entry': 120000., 'entry_ms': study.M, 'entry_fee': .06}
        with patch.multiple(study.core, START=0, BEGIN=0, END=n*study.M), patch.object(study.execution, 'outcome', return_value=proposed):
            rows, account = study.replay([{'T': 0, 'side': 1, 'atr_pct': .005}], trade, trade, [], 'BTCUSDT', False)
        self.assertEqual(rows[0]['rejection'], 'notional_cap_100')
        self.assertEqual(account['closed'], 0)
        self.assertEqual(account['cash'], 1000)

    def test_real_outcome_and_account_conserve_cash_on_flat_prices(self):
        n = 300; trade = np.tile([2000., 2000., 2000., 2000.], (n, 1))
        with patch.multiple(study.core, START=0, BEGIN=0, END=n*study.M), patch.object(study.execution.r, 'START', 0):
            rows, account = study.replay([{'T': 0, 'side': 1, 'atr_pct': .005}], trade, trade, [], 'ETHUSDT', False)
        outcome = rows[0]['outcome']
        self.assertEqual(outcome['reason'], 'time')
        self.assertEqual(account['closed'], 1)
        self.assertLess(account['net'], 0)
        self.assertAlmostEqual(account['cash'], 1000+account['net'])
        self.assertGreaterEqual(account['drawdown'], -account['net']/1000)


if __name__ == '__main__':
    unittest.main()
