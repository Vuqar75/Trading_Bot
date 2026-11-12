import unittest
import numpy as np
from research.pattern_direction_features import transform,FEATURES,SIGNED
from research.technical_patterns import FEATURES as RAW


class DirectionFeaturesTest(unittest.TestCase):
    def cases(self):
        x=np.zeros((2,len(RAW)),dtype=np.float32)
        x[:,30]=[1,-1];x[:,31]=1;x[0,22]=1;x[1,23]=1
        return x

    def test_mirrored_directions_have_same_aligned_market_features(self):
        x=self.cases()
        for name in SIGNED:x[:,RAW.index(name)]=[.02,-.02]
        x[:,4]=[.75,.25];x[:,8]=[.1,.3];x[:,9]=[.3,.1];x[:,10]=[.75,.25]
        out=transform(x)
        np.testing.assert_array_equal(out[0,:26],out[1,:26])
        self.assertEqual(out[:,26].tolist(),[1,-1])
        self.assertEqual(out.shape,(2,len(FEATURES)));self.assertEqual(len(FEATURES),30)

    def test_input_not_mutated_and_unscaled_context_preserved(self):
        x=self.cases();x[:,5]=.003;x[:,6]=.2;x[:,15]=1.5;x[:,20]=.4
        before=x.copy();out=transform(x)
        np.testing.assert_array_equal(x,before)
        for i in (5,6,15,20):np.testing.assert_array_equal(out[:,i],x[:,i])
        np.testing.assert_array_equal(out[:,27:],x[:,31:])

    def test_contradictory_missing_or_wrong_direction_flags_rejected(self):
        for change in ('contradict','missing','side','asset','nan'):
            x=self.cases()
            if change=='contradict':x[0,23]=1
            elif change=='missing':x[0,22]=0
            elif change=='side':x[0,30]=0
            elif change=='asset':x[0,32]=1
            else:x[0,0]=np.nan
            with self.assertRaises(ValueError):transform(x)

    def test_rows_independent_and_empty_shape_supported(self):
        x=self.cases();first=transform(x[:1]);x[1,0]=999
        np.testing.assert_array_equal(first,transform(x)[:1])
        self.assertEqual(transform(np.empty((0,34))).shape,(0,30))

    def test_nonoverlap_screen_uses_fixed_label_end_and_does_not_make_pnl(self):
        from research.pattern_direction_experiment import nonoverlap_cases
        bar=900000
        inputs={'T':np.array([0,bar,5*bar,0,0,bar]),'label_end_ms':np.array([4*bar,5*bar,9*bar,4*bar,4*bar,5*bar]),
                'symbol':np.array([0,0,0,1,2,2]),'side':np.array([1,-1,-1,1,-1,1]),'stress_target':np.array([.01,-.02,.02,-.01,.005,-.02])}
        p=np.full(6,.7);p[3]=.59
        result,kept=nonoverlap_cases(inputs,p,np.full(6,.001),np.arange(6))
        self.assertEqual(kept.tolist(),[0,4,2]);self.assertEqual(result['nonoverlap_cases'],3)
        self.assertFalse(result['screen_passed']);self.assertFalse(result['executed_trades']);self.assertIsNone(result['portfolio_pnl'])

    def test_quality_screen_requires_both_metrics_and_asset_guard(self):
        from research.pattern_direction_experiment import quality
        import copy
        values=lambda n:{k:{'net_mse':n,'brier':n} for k in ('rf','global','asset')}
        ref={'total':values(1.),'by_asset':{a:values(1.) for a in ('BTCUSDT','ETHUSDT','SOLUSDT')},'by_month':{str(i):values(1.) for i in range(16)}}
        candidate={'total':values(.98),'by_asset':{a:values(.98) for a in ref['by_asset']},'by_month':{m:values(.98) for m in ref['by_month']}}
        self.assertTrue(quality(candidate,ref)['screen_passed'])
        bad=copy.deepcopy(candidate);bad['by_asset']['ETHUSDT']['rf']['net_mse']=1.03
        self.assertFalse(quality(bad,ref)['screen_passed'])
        bad=copy.deepcopy(candidate);bad['total']['rf']['brier']=1.
        self.assertFalse(quality(bad,ref)['screen_passed'])
