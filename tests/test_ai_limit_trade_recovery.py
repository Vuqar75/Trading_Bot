"""Source recovery on temporary ledgers; no network, model or working orders."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.ai_limit_book import Book
from research.ai_limit_feed import Feed
from research.ai_limit_runtime import Poller,analyst
from tests.test_ai_limit_brackets import snapshot,T


class TradeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=T
        self.book=Book(Path(self.temp.name)/'paper',clock=lambda:self.now)
        self.book.funding(T,T,[]);self.book.pause(False);self.now+=100;self.requests=[];self.batches=[]
        def fetch(path,params):
            self.assertEqual(path,'/fapi/v1/aggTrades');self.requests.append(dict(params))
            return self.batches.pop(0)
        self.feed=Feed(fetch=fetch,clock=lambda:self.now)
        self.feed.book=lambda symbol:({**snapshot()[0]['quote'],'id':str(self.now),'event_ms':self.now,'received_ms':self.now},self.now+100000)
        self.feed.filters=lambda symbol:{**snapshot()[0]['filters'],'as_of_ms':self.now}
        self.poller=Poller(self.book,self.feed,symbols=('BTCUSDT',))
        # Funding is independently covered by existing bracket tests. Isolate
        # source admission deterministically without a background worker.
        self.poller.funding_ready=lambda:None
    def tearDown(self):self.poller.close();self.book.close();self.temp.cleanup()
    def row(self,i,age=0,price='100',qty='1'):
        return {'a':i,'T':self.now-age,'p':price,'q':qty,'m':True}
    def accept(self):
        s,d=snapshot();self.book.accept(s,d,provenance={'kind':'fixture','response_id':'recovery','model':'fixture'})
    def test_idle_backlog_reseeds_without_fills_then_requires_fresh_contiguous_poll(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[[self.row(10,6000)],[self.row(100,6000),self.row(101)]]
        result=self.poller.cycle()
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),101)
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_trade_stream_unconfirmed')
        self.assertEqual(result['fills'],0);self.assertEqual(self.requests[0]['fromId'],10)
        self.assertNotIn('fromId',self.requests[1])
        report=result['last_trade_gap']['source'];self.assertEqual(report['stale_rows'],1)
        self.assertEqual(report['discontinuities'],[])
        self.assertEqual(result['trade_feeds']['BTCUSDT']['idle_resync']['last_id'],101)
        self.batches=[[]];self.poller.cycle()
        self.assertEqual(self.book.get('market_error:BTCUSDT'),'public_trade_stream_unconfirmed')
        self.now+=100;self.batches=[[self.row(102)]];result=self.poller.cycle()
        self.assertIsNone(result['market_errors']['BTCUSDT']);self.assertEqual(result['fills'],0)
        self.assertEqual(self.requests[-1]['fromId'],102)
    def test_partial_position_gap_never_resyncs_and_protection_still_closes(self):
        self.accept();self.now+=100;self.book.market('trade',{'id':'partial','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('trade_cursor:BTCUSDT',9)
        self.now+=100;self.batches=[[self.row(12)]];result=self.poller.cycle()
        state=self.book.active()[1]
        self.assertEqual(state['remaining'],'0');self.assertEqual(state['open_qty'],'0.2')
        self.assertEqual(state['entry_cancellation']['reason'],'TRADE_GAP')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(result['last_trade_gap']['source']['discontinuities'],[{'expected':10,'received':12}])
        self.now+=100
        q={**snapshot()[0]['quote'],'id':'stop','event_ms':self.now,'received_ms':self.now,'bid':'94','ask':'94.1','mark':'94'}
        self.book.market('quote',q)
        self.assertEqual(self.book.active()[1]['open_qty'],'0');self.assertTrue(self.book.verify()['balanced'])
    def test_stale_active_trade_cancels_without_historical_fill(self):
        self.accept();self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[[self.row(10,6000,'98.9')],[self.row(100)]]
        result=self.poller.cycle()
        self.assertEqual(result['fills'],0);self.assertIsNone(self.book.active())
        self.assertEqual(self.book.plans()[0][1]['status'],'CANCELED')
        self.assertIsNotNone(result['market_errors']['BTCUSDT'])
        self.assertEqual(self.book.balance('reserved'),0)
    def test_sequence_and_age_diagnostics_are_distinct_and_bounded(self):
        self.batches=[[self.row(10,6000),self.row(12,-1),self.row(13)]]
        events,last,gap=self.feed.trades('BTCUSDT',9);r=self.feed.trade_report['BTCUSDT']
        self.assertTrue(gap);self.assertEqual(last,13);self.assertEqual([e['id'] for e in events],['13'])
        self.assertEqual((r['stale_rows'],r['future_rows']),(1,1));self.assertEqual(r['fetch_ms'],0)
        self.assertEqual(r['oldest_age_ms'],6000);self.assertEqual(r['newest_age_ms'],-1)
        self.assertEqual(len(r['batch_hash']),64)
    def test_stalled_empty_stream_cannot_remain_healthy(self):
        self.batches=[[self.row(10)]];self.poller.cycle()
        self.assertIsNone(self.book.get('market_error:BTCUSDT'))
        self.now+=5001;self.batches=[[]];self.poller.cycle()
        self.assertEqual(self.book.get('market_error:BTCUSDT'),'public_trade_stream_unconfirmed')
    def test_gap_blocks_paid_collection_before_snapshot_or_model(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[[self.row(10,6000)],[self.row(100)]];status=self.poller.cycle()
        status['funding_through_ms']=self.now
        with patch('research.ai_limit_runtime.trusted',return_value=status),patch.object(self.feed,'snapshot',side_effect=AssertionError('must not collect')),patch('research.ai_limit_runtime.once',side_effect=AssertionError('must not call model')):
            result=analyst(None,None,None,symbol='BTCUSDT',paper_uid=0,feed=self.feed,clock=lambda:self.now)
        self.assertEqual(result['status'],'not_requested_market_or_funding_unconfirmed')
    def test_transport_failure_needs_new_trade_not_empty_poll_to_recover(self):
        self.batches=[[self.row(10)]];self.poller.cycle()
        self.assertIsNone(self.book.get('market_error:BTCUSDT'))
        with patch.object(self.feed,'fetch',side_effect=OSError('fixture-offline')):
            self.poller.cycle()
        self.batches=[[]];self.poller.cycle()
        self.assertEqual(self.book.get('market_error:BTCUSDT'),'public_trade_stream_unconfirmed')
        self.batches=[[self.row(11)]];self.poller.cycle()
        self.assertIsNone(self.book.get('market_error:BTCUSDT'))
    def test_failed_resync_retains_original_cursor_and_error(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[[self.row(10,6000)],[self.row(8)]]
        result=self.poller.cycle()
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),9)
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_market_unconfirmed')
        self.assertEqual(result['last_trade_gap']['source']['last_id'],10)


if __name__=='__main__':unittest.main()
