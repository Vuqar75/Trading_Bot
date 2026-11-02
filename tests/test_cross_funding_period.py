import unittest
from trading_bot.cross_funding_period import assign_period,require_unique_regular


class FundingPeriodTests(unittest.TestCase):
    def test_half_open_boundaries_preserve_original_event_time(self):
        windows=[(1000,1100),(1100,1200)]
        self.assertEqual(assign_period(999,[(900,1000),*windows],opened_ms=1),900)
        self.assertEqual(assign_period(1000,windows,opened_ms=1),1000)
        self.assertEqual(assign_period(1001,windows,opened_ms=1),1000)
        self.assertEqual(assign_period(1100,windows,opened_ms=1),1100)
        with self.assertRaisesRegex(ValueError,'missing'):assign_period(999,windows,opened_ms=1)

    def test_position_boundary_and_overlapping_evidence_block(self):
        self.assertEqual(assign_period(1001,[(1000,1100)],opened_ms=1001),1000)
        with self.assertRaisesRegex(ValueError,'ownership'):assign_period(1001,[(1000,1100)],opened_ms=1,closed_ms=1001)
        with self.assertRaisesRegex(ValueError,'ambiguous'):assign_period(1001,[(1000,1100),(1000,1050)],opened_ms=1)

    def test_missing_duplicate_and_late_events_fail_closed(self):
        event={'period_due':1000,'reported_time':1001,'rate_type':'Regular'}
        require_unique_regular([event],1000,1100)
        with self.assertRaisesRegex(ValueError,'not published'):require_unique_regular([],1000,1100)
        with self.assertRaisesRegex(ValueError,'Duplicate'):require_unique_regular([event,event],1000,1100)
        with self.assertRaisesRegex(ValueError,'ambiguous'):require_unique_regular([event,dict(event,rate_type='Special')],1000,1100)
        with self.assertRaisesRegex(ValueError,'ambiguous'):require_unique_regular([dict(event,reported_time=1100)],1000,1100)


if __name__=='__main__':unittest.main()
