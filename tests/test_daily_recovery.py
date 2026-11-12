import unittest
import numpy as np
from research import daily_recovery as p


class RecoveryTests(unittest.TestCase):
    def fixture(self):
        close=np.repeat([80.,90.,100.],1440)
        t=2*1440+5*60
        close[t+10]=92.;close[t+11]=95.
        close[t+20]=92.;close[t+21]=95.
        return np.column_stack((close,close*1.01,close*.99,close)),t

    def test_daily_not_hourly_context_and_first_recovery_only(self):
        trade,t=self.fixture();data=p.features(trade,0,daily_window=2)
        self.assertIn(t+12,data['events'])
        self.assertNotIn(t+22,data['events'])
        c=data['events'][t+12]
        self.assertEqual(c['daily_sma200'],85.)
        self.assertEqual(c['hour_source_end_ms'],(t//60)*60*p.M)
        self.assertEqual(c['entry_side'],1)
        self.assertLess(c['partial_rsi_previous'],5)
        self.assertGreaterEqual(c['partial_rsi'],5)
        with self.assertRaisesRegex(ValueError,'warmup'):p.features(trade,0)

    def test_future_hour_day_and_truncated_prefix_do_not_change_decision(self):
        trade,t=self.fixture();expected=p.features(trade,0,2)['events'][t+12]
        changed=trade.copy();changed[t+12:]=changed[t+12:]*1.3
        self.assertEqual(p.features(changed,0,2)['events'][t+12],expected)
        prefix=p.features(trade[:t+12],0,2)
        self.assertEqual(prefix['events'][t+12],expected)

    def test_short_is_symmetric_and_uses_completed_daily_context(self):
        trade,t=self.fixture();mirror=200-trade[:,[0,2,1,3]]
        c=p.features(mirror,0,2)['events'][t+12]
        self.assertEqual(c['entry_side'],-1);self.assertEqual(c['daily_sma200'],115.)

    def context(self):
        return {'expiry_ms':60*p.M,'daily_sma200':.8,'entry_mean':1.1,'atr':.03}

    def test_volatility_stop_keeps_actual_dollar_risk_and_symbol_cash(self):
        Account,_=p.account_class();a=Account('ADAUSDT',False)
        a.contexts[0]=self.context();a.enter(p.M,1,1.,1.,0)
        self.assertIsNotNone(a.position)
        self.assertEqual(a.position['stop_pct'],.05)
        self.assertLessEqual(a.position['estimated_risk'],5.)
        self.assertLess(a.position['qty'],100)
        self.assertAlmostEqual(a.position['stop'],a.position['entry']*.95)
        a.close(2*p.M,1.01,'time');a.symbol='DOTUSDT'
        a.contexts[2*p.M]={'expiry_ms':60*p.M,'daily_sma200':10.,'entry_mean':7.,'atr':.08}
        a.enter(3*p.M,-1,8.,8.,2*p.M);a.close(4*p.M,7.95,'time')
        self.assertEqual({x['symbol'] for x in a.trades},{'ADAUSDT','DOTUSDT'})
        self.assertAlmostEqual(a.cash,1000+sum(x['net'] for x in a.trades))

    def test_expired_or_gap_entry_does_not_charge_fee(self):
        Account,_=p.account_class()
        for t,price in ((60*p.M,1.),(p.M,1.1),(p.M,.7)):
            a=Account('ADAUSDT',False);a.contexts[0]=self.context();a.enter(t,1,price,price,0)
            self.assertIsNone(a.position);self.assertEqual(a.cash,1000)


if __name__=='__main__':unittest.main()
