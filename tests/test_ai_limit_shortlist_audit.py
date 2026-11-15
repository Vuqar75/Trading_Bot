import unittest
from research.ai_limit_shortlist_audit import compare,crossing
class ShortlistAuditTests(unittest.TestCase):
 def test_nearer_omission_compares_only_same_entry_stop_direction(self):
  c={'direction':'LONG','entry_level':'e','stop_level':'s','target_level':'far','entry':'100','stop':'95','target':'120'}
  near={**c,'target_level':'near','target':'110'};other={**near,'stop_level':'other'}
  snap={'candidate_plans':[c],'levels':[{'kind':'resistance','price':'108'},{'kind':'resistance','price':'108'},{'kind':'support','price':'105'}]}
  result=compare(snap,[c,near,other])[0]
  self.assertEqual(result['nearer_valid_omitted'],[near]);self.assertEqual(result['intermediate_prices'],['108'])
  snap['candidate_plans'].append(near)
  self.assertFalse(compare(snap,[c,near])[0]['nearer_valid_omitted'])
 def test_short_uses_support_and_strict_padded_target_boundaries(self):
  snap={'levels':[{'kind':'support','price':p} for p in ['100','95','90']]+[{'kind':'resistance','price':'96'}]}
  c={'direction':'SHORT','entry':'100','target':'90'}
  self.assertEqual(crossing(snap,c),['95'])

 def test_enrichment_rejects_context_not_actually_sent(self):
  from research.ai_limit_shortlist_audit import enrich
  with self.assertRaisesRegex(ValueError,'saved_request_context_mismatch'):
   enrich({}, {'snapshot_id':'original'}, {'snapshot_id':'other'})
