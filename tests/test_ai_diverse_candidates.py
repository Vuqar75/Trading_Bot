import copy,random,unittest
from decimal import Decimal
from research.ai_limit_plan import select_candidates,CANDIDATE_POLICY,LEGACY_CANDIDATE_POLICY,context,validate,canonical,request
from research.ai_limit_demo import capture,book_quote
from tests.test_ai_limit_brackets import T
class DiverseCandidatesTests(unittest.TestCase):
 def c(self,e,s,t,i):return dict(direction='LONG',entry=str(e),stop=str(s),target=str(t),entry_level='e'+str(e),stop_level='s'+str(s),target_level=i,net_reward_risk=str((t-e)/(e-s)))
 def test_nearest_valid_per_numeric_pair_before_extras_and_dedup(self):
  a=self.c(99,97,110,'a');far=self.c(99,97,120,'b');b=self.c(98,96,109,'c')
  duplicate={**a,'target_level':'z'}
  out=select_candidates([far,duplicate,b,a],Decimal(100))
  self.assertEqual([x['target_level'] for x in out],['a','c','b'])
 def test_distinct_pairs_fill_first_and_shuffle_invariant(self):
  pool=[self.c(e,e-2,e+distance,str(e)+'-'+str(distance)) for e in [99,98,97,96,95] for distance in [10,20]]
  expected=select_candidates(pool,Decimal(100));self.assertEqual(len({(x['entry'],x['stop']) for x in expected}),4)
  self.assertTrue(all(Decimal(x['target'])-Decimal(x['entry'])==10 for x in expected))
  for seed in range(10):
   shuffled=pool[:];random.Random(seed).shuffle(shuffled);self.assertEqual(select_candidates(shuffled,Decimal(100)),expected)
 def test_policy_hash_old_context_and_unchanged_guards(self):
  cap=capture(T);q=book_quote(T);f={'as_of_ms':T,'tick_size':'.1','step_size':'.001','min_qty':'.001','min_notional':'50'};news={'coverage':'limited','high_risk_until_ms':0,'items':[]}
  old=context(cap,q,f,news,T,candidate_policy=LEGACY_CANDIDATE_POLICY);new=context(cap,q,f,news,T)
  self.assertNotIn('candidate_policy',old);self.assertEqual(new['candidate_policy'],CANDIDATE_POLICY);self.assertNotEqual(old['snapshot_id'],new['snapshot_id']);self.assertEqual(old['levels'],new['levels'])
  self.assertEqual(old,context(cap,q,f,news,T,candidate_policy=LEGACY_CANDIDATE_POLICY))
  for c in new['candidate_plans']:
   d=dict(snapshot_id=new['snapshot_id'],decision=c['direction'],entry_level=c['entry_level'],stop_level=c['stop_level'],target_level=c['target_level'],valid_until_ms=new['expires_ms']-1,max_hold_minutes=60,thesis='fixture',news_ids=[],risks=[])
   v=validate(new,d,T);self.assertLessEqual(Decimal(v['risk_usdt']),5);self.assertLessEqual(Decimal(v['quantity'])*Decimal(v['entry']),100)
  self.assertLessEqual(len(canonical(new).encode()),16000);request(new)
