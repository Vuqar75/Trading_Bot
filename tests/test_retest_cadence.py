import unittest
from unittest.mock import patch
import numpy as np
from research import retest_cadence as cadence

class RetestTest(unittest.TestCase):
    def test_confirmation_uses_separate_post_breakout_bars_and_mirrors(self):
        a=np.tile([100.,101.,99.,100.],(1600,1))
        a[1499]=[100,106,100,105]
        a[1500:1515]=[105,105,99,102]
        a[1515:1530]=[102,107,101,106]
        mirrored=200-a[:,[0,2,1,3]]
        with patch.object(cadence,'BEGIN',1500):
            for prices,side in [(a,1),(mirrored,-1)]:
                self.assertEqual(cadence.scenarios(prices[:1530]),[])
                row=cadence.scenarios(prices)[0]
                self.assertEqual((row['start'],row['touch'],row['at'],row['side']),(1500,1515,1530,side))
                self.assertEqual(row['target'],106 if side==1 else 94)
