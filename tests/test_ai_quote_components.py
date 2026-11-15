import unittest
from research.ai_limit_feed import Feed
class QuoteComponentsTests(unittest.TestCase):
 def test_stale_component_is_identified_without_accepting_quote(self):
  now=100000
  book={'symbol':'BTCUSDT','time':now-6000,'bidPrice':'100','askPrice':'101','bidQty':'1','askQty':'1'}
  mark={'symbol':'BTCUSDT','time':now,'markPrice':'100','nextFundingTime':now+1000}
  f=Feed(fetch=lambda path,params:book if path.endswith('bookTicker') else mark,clock=lambda:now)
  with self.assertRaisesRegex(ValueError,'stale_or_future_market_data'):f.book('BTCUSDT')
  self.assertEqual(f.quote_report['BTCUSDT']['time_rejections'],['book_stale'])
  book['time']=now;mark['time']=now+1
  with self.assertRaisesRegex(ValueError,'stale_or_future_market_data'):f.book('BTCUSDT')
  self.assertEqual(f.quote_report['BTCUSDT']['time_rejections'],['mark_future'])

 def test_cancellation_keeps_its_quote_evidence_after_next_error(self):
  import tempfile
  from pathlib import Path
  from research.ai_limit_book import Book
  from tests.test_ai_limit_brackets import T,snapshot
  with tempfile.TemporaryDirectory() as temp:
   b=Book(Path(temp)/'paper',clock=lambda:T+100)
   try:
    b.funding(T,T+100,[]);b.pause(False);s,d=snapshot();b.accept(s,d,provenance={'kind':'fixture','response_id':'quote-evidence','model':'fixture'})
    evidence={'at_ms':T+100,'kind':'failed_http_or_validation','quote_age_ms':3226,'quote_components':{'book_source_ms':T,'mark_source_ms':T-6000,'time_rejections':['mark_stale']}}
    b.set('last_quote_unconfirmed:BTCUSDT',evidence);b.hold_unconfirmed_backlog('BTCUSDT',detail='quote_http_unconfirmed')
    b.set('last_quote_unconfirmed:BTCUSDT',{'at_ms':T+101})
    self.assertEqual(b.plans()[0][1]['entry_cancellation']['quote_diagnostic'],evidence);self.assertTrue(b.verify()['balanced'])
   finally:b.close()
