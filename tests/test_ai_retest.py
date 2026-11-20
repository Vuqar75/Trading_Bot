import unittest
from decimal import Decimal as D
from research.ai_limit_retest import annotate


class RetestTests(unittest.TestCase):
    def test_entry_bar_target_ignored_and_ambiguous_later_stop_first(self):
        snap = {'quote':{'ask':'100','bid':'100'},'levels':[]}
        c = {'direction':'LONG','entry':'99','target':'103','stop':'97'}
        def bar(lo,hi):
            return D(lo)/100,D(hi)/100
        flat = bar(99,101)
        good = [bar(98,104),bar(99,104),flat,flat]
        early = [bar(98,104),flat,flat,flat]
        ambiguous = [bar(98,100),bar(96,104),flat,flat]
        touched = [bar(99,101),bar(99,104),flat,flat]
        self.assertIsNone(annotate(c,snap,[good,good,early,ambiguous,touched]))
        result=annotate(c,snap,[good]*3+[early,ambiguous,touched])
        self.assertEqual(result['analog_counts'],[6,5,3])

    def test_short_obstacle_order_and_strict_target(self):
        snap={'quote':{'ask':'100','bid':'100'},'levels':[
            {'kind':'support','price':'98'},{'kind':'support','price':'99'}]}
        c={'direction':'SHORT','entry':'101','target':'97','stop':'105'}
        first=(D('1'),D('1.02'))
        good=[first,(D('.96'),D('1')),first,first]
        touch=[first,(D('.97'),D('1')),first,first]
        result=annotate(c,snap,[good]*3+[touch])
        self.assertEqual(result['analog_counts'],[4,4,3])
        self.assertEqual(result['first_obstacle'],'99')
