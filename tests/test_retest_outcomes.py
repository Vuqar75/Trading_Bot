import unittest
from decimal import Decimal as D

from research.retest_outcomes_audit import exits, funding_range, price_result, BAR


class OutcomeTests(unittest.TestCase):
    # Signed normalized prices. Passive entry below 100, SL 95, target 105.
    entry = (101,102,99,100)
    quiet = (100,104,96,100)

    def reasons(self, bars):
        return {r for r,*_ in exits(bars,100,95,105)}

    def test_preentry_high_and_close_order(self):
        last = (100,103,98,100)
        self.assertEqual(self.reasons([(101,106,99,100)]+[self.quiet]*3+[last]), {'TP','TIME'})
        self.assertEqual(self.reasons([(101,106,99,106)]+[self.quiet]*3+[last]), {'TP'})
        self.assertEqual(self.reasons([(101,106,100,101)]),set())

    def test_final_partial_bar_and_ambiguous_orders(self):
        base=[self.entry]+[self.quiet]*3
        self.assertEqual(self.reasons(base),{'CENSORED'})
        self.assertEqual(self.reasons(base+[(100,106,94,101)]),{'TIME','SL','TP'})
        self.assertEqual(self.reasons([self.entry,(100,106,94,100)]),{'TP','SL'})
        self.assertEqual(list(exits(base+[(90,102,89,100)],100,95,105)),[('SL',4,90,90)])

    def test_sides_costs_funding_and_no_double_charge(self):
        for side in [1,-1]:
            q=D('.001')
            v=price_result(side,D(100),q,D(101)*side,'TIME',D(1),D(0))
            fill=D(101)*(1-D('.001')*side)
            self.assertEqual(v['net'],q*(side*(fill-100)-D('.0005')*(100+fill)))
            tp=price_result(side,D(100),q,D(101)*side,'TP',D(1),D(0))
            self.assertEqual(tp['impact'],0)
            amount=-side*q*D(100)*D('.001')
            for stamp, expected in [(0,(0,0)),(BAR+12,(amount,amount)),
                                    (4*BAR+10,(min(0,amount),max(0,amount)))]:
                self.assertEqual(funding_range([(stamp,D(100),D('.001'))],side,q,1,0,BAR-1,4,'TIME'),expected)
