import copy
import unittest
from research import panel_net_calibration as c


class CalibrationTests(unittest.TestCase):
    def fixture(self):
        predictions=[];rows=[]
        for day in range(40):
            for hour in range(8):
                t=day*c.DAY+hour*c.p.H
                predictions.append({'T':t,'symbol':'ADAUSDT','model':'HGBR','long':.003,'short':-.01})
                rows.append({'T':t,'symbol':'ADAUSDT','label_end_ms':t+246*c.p.M,
                             'labels':{'1':{'net_return':.001},'-1':{'net_return':-.01}}})
        return predictions,rows

    def test_correction_is_known_daily_bias_and_never_increases_forecast(self):
        pred,rows=self.fixture();adjusted=c.correct(pred,rows)
        q=next(r for r in adjusted if r['T']==20*c.DAY+5*c.p.H)
        self.assertAlmostEqual(q['long'],.001)
        self.assertLessEqual(q['calibration']['1']['latest_label_end_ms'],q['T'])
        self.assertFalse(adjusted[0]['calibration']['1']['ready'])
        self.assertEqual(q['short'],-1.)
        changed=copy.deepcopy(rows)
        for r in changed:r['labels']['1']['net_return']=.1
        q=c.correct(pred,changed)[165]
        self.assertEqual(q['long'],.003)

    def test_future_labels_cannot_change_earlier_corrected_decision(self):
        pred,rows=self.fixture();t=20*c.DAY+5*c.p.H
        expected=next(r for r in c.correct(pred,rows) if r['T']==t)
        changed=copy.deepcopy(rows)
        for r in changed:
            if r['label_end_ms']>t:r['labels']['1']['net_return']=-100.
        actual=next(r for r in c.correct(pred,changed) if r['T']==t)
        self.assertEqual(actual,expected)
        truncated=[r for r in changed if r['T']<=t]
        actual=next(r for r in c.correct([r for r in pred if r['T']<=t],truncated) if r['T']==t)
        self.assertEqual(actual,expected)


if __name__=='__main__':unittest.main()
