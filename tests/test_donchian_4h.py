import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from research import donchian_4h as d

class DonchianTest(unittest.TestCase):
    def test_excludes_signal_bar_and_current_unfinished_bar(self):
        p=np.tile([60000.,60010.,59990.,60000.],(22*d.BAR,1))
        p[21*d.BAR-1]=[60000,60200,59990,60100]
        self.assertEqual(list(d.signals(p[:21*d.BAR-1])),[])
        at,long=next(d.signals(p))
        self.assertEqual((at,long['side'],long['high20']),(21*d.BAR,1,60010))
        self.assertEqual(long['exits'],[-1])
        _,short=next(d.signals(120000-p[:,[0,2,1,3]]))
        self.assertEqual(short['side'],-1)
        self.assertAlmostEqual(short['atr'],long['atr'])

    def test_stop_takes_precedence_at_scheduled_exit_open(self):
        p=np.tile([60000.,60001.,59999.,60000.],(500,1))
        decision=dict(side=1,exits=[],atr=100.,regime='range')
        reverse=dict(decision,side=-1,exits=[1])
        p[241]=[59499,59500,59498,59499]
        with tempfile.TemporaryDirectory() as temp, patch.object(d,'OUT',Path(temp)), patch.object(d,'signals',return_value=[(0,decision),(240,reverse)]):
            result=d.replay(d.av.core.START,p,p,[],False,0,'unit')
        t,=result['trades']
        self.assertEqual((t['reason'],t['side']),('SL',1))
        self.assertAlmostEqual(t['exit'],59499*.9995)
        self.assertAlmostEqual(t['net'],t['gross']-t['fees']-t['impact']+t['funding'])
        self.assertEqual(len(result['attempts']),1)
