import unittest
from trading_bot.cross_funding_coverage import check_funding_window


class CrossFundingCoverageTests(unittest.TestCase):
    def args(self):
        return {'prior_due':1000,'prior_interval':100,'server_ms':999,'observed_due':1000,'observed_interval':100,'events':[]}

    def test_before_deadline_and_published_payment(self):
        args=self.args();self.assertTrue(check_funding_window(**args)['complete'])
        args.update(server_ms=1002,observed_due=1100,events=[{'time':1000,'rate_type':'Regular'},{'time':1000,'rate_type':'Special'}])
        self.assertEqual(check_funding_window(**args)['next_due'],1100)

    def test_delayed_missing_duplicate_and_shift_are_not_complete(self):
        args=self.args();args.update(server_ms=1002,observed_due=1100)
        with self.assertRaisesRegex(ValueError,'not published'):check_funding_window(**args)
        args['events']=[{'time':1000},{'time':1000}]
        with self.assertRaisesRegex(ValueError,'duplicate'):check_funding_window(**args)
        args['events']=[{'time':1000}];args['observed_interval']=50
        with self.assertRaisesRegex(ValueError,'interval changed'):check_funding_window(**args)
        args['observed_interval']=100;args['server_ms']=1100
        with self.assertRaisesRegex(ValueError,'More than one'):check_funding_window(**args)
        args=self.args();args['observed_due']=900
        with self.assertRaisesRegex(ValueError,'changed'):check_funding_window(**args)


if __name__=='__main__':unittest.main()
