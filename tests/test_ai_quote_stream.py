"""USD-M quote/protection fixtures only; no actual sockets, model or orders."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from research.ai_limit_book import Book
from research.ai_limit_runtime import Poller
from research.ai_limit_stream import StreamFeed
from tests.test_ai_limit_brackets import T, snapshot


class NoNetworkExecutor:
    def submit(self,*a,**k):raise AssertionError('REST must not be submitted for WS protection')
    def shutdown(self,**k):pass


class QuoteStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=T+100
        self.root=Path(self.temp.name)/'paper';self.b=Book(self.root,clock=lambda:self.now)
        self.b.funding(T,self.now,[]);self.b.pause(False)
        self.f=StreamFeed(symbols=['BTCUSDT'],quotes=True,clock=lambda:self.now)
        self.f.book=lambda *_:(_ for _ in ()).throw(AssertionError('unexpected REST'))
        self.f.quote_connection('BTCUSDT','market',True);self.f.quote_connection('BTCUSDT','book',True)
        self.f.states['BTCUSDT'].update(connected=True,fault=False)
        self.b.set('trade_cursor:BTCUSDT',1)
        self.poller=Poller(self.b,self.f,symbols=['BTCUSDT'],quote_executor=NoNetworkExecutor())
        self.poller.filters_ready=lambda _:True;self.poller.funding_ready=lambda:None
    def tearDown(self):self.poller.close();self.b.close();self.temp.cleanup()
    def book(self,u=100,price='100',qty='2',stamp=None):
        stamp=self.now if stamp is None else stamp
        raw={'e':'bookTicker','s':'BTCUSDT','u':u,'E':stamp,'T':stamp,'b':price,'a':price,'B':qty,'A':qty}
        self.f.receive_quote('BTCUSDT',raw,'book');return raw
    def mark(self,price='100',stamp=None):
        raw={'e':'markPriceUpdate','s':'BTCUSDT','E':self.now if stamp is None else stamp,'p':price,'T':T+86400000}
        self.f.receive_quote('BTCUSDT',raw,'market');return raw
    def opened(self,side='SHORT',qty='.2'):
        s,d=snapshot(side);self.b.accept(s,d,provenance={'kind':'fixture','response_id':'ws-quote','model':'fixture'})
        p=self.b.active()[0];self.now+=1
        self.b.market('trade',{'id':'entry','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,
            'price':'101.1' if side=='SHORT' else '98.9','qty':qty,'buyer_maker':side=='LONG'})
        return p
    def test_stream_quote_ignores_rest_and_source_times_are_not_receipt_times(self):
        self.book();self.mark();start=time.monotonic();r=self.poller.cycle()
        self.assertLess(time.monotonic()-start,1);self.assertFalse(self.poller.quote_jobs)
        self.assertIsNone(self.b.get('quote_execution_unconfirmed:BTCUSDT'))
        self.assertEqual(self.b.get('quote:BTCUSDT')['event_ms'],self.now)
        self.now+=6000;self.mark();self.poller.cycle()
        self.assertEqual(self.b.get('quote_execution_unconfirmed:BTCUSDT'),'public_quote_stream_unconfirmed')
        report=self.b.get('quote_stream:BTCUSDT');self.assertIn('book_source_ms_stale',report['time_rejections'])
    def test_a_never_completed_rest_future_cannot_delay_the_ws_stop(self):
        from concurrent.futures import Future
        self.opened();self.poller.quote_jobs['BTCUSDT']=(Future(),time.monotonic_ns())
        self.now+=1;self.book(101,'104');self.mark('104');start=time.monotonic();self.poller.cycle()
        self.assertLess(time.monotonic()-start,1);self.assertEqual(self.b.plans()[0][1]['open_qty'],'0')
        self.assertFalse(self.poller.quote_jobs['BTCUSDT'][0].done());self.assertTrue(self.b.verify()['balanced'])
    def test_fresh_mark_latches_stop_while_book_stale_partial_exit_reuses_no_liquidity(self):
        self.opened();self.book();self.mark();self.poller.cycle()
        self.now+=6001;self.mark('104');self.poller.cycle();state=self.b.active()[1]
        self.assertEqual(state['close_reason'],'SL');self.assertEqual(state['protection_trigger']['observed_ms'],self.now)
        self.assertEqual(state['protection_trigger']['component']['kind'],'mark')
        self.assertEqual(state['status'],'EXIT_PENDING');self.assertEqual(self.b.snapshot()['fills'],1)
        trigger=state['protection_trigger'];self.book(101,'104','.1');self.poller.cycle()
        self.assertEqual(self.b.active()[1]['open_qty'],'0.1');self.assertEqual(self.b.snapshot()['fills'],2)
        self.now+=1000;self.mark('105');self.poller.cycle()
        self.assertEqual(self.b.snapshot()['fills'],2) # new mark != new BBO liquidity
        self.b.close();self.b=Book(self.root,clock=lambda:self.now);self.poller.book=self.b
        self.poller.cycle();self.assertEqual(self.b.snapshot()['fills'],2)
        self.now+=1;self.book(102,'105','.1');self.poller.cycle();state=self.b.plans()[0][1]
        self.assertEqual(state['open_qty'],'0');self.assertEqual(state['protection_trigger'],trigger)
        self.assertEqual(self.b.snapshot()['fills'],3);self.b.pulse();self.assertEqual(self.b.snapshot()['fills'],3)
        self.assertTrue(self.b.verify()['balanced'])
    def test_fresh_book_latches_long_stop_without_mark(self):
        self.opened('LONG');self.book();self.mark();self.poller.cycle()
        self.now+=6001;self.book(101,'96');self.poller.cycle();state=self.b.active()[1]
        self.assertEqual(state['protection_trigger']['component']['kind'],'book')
        self.assertEqual(state['close_reason'],'SL');self.assertEqual(self.b.snapshot()['fills'],1)
        self.mark('96');self.poller.cycle();self.assertEqual(self.b.plans()[0][1]['open_qty'],'0')
    def test_time_is_detected_during_total_market_outage_then_executed_only_on_recovery(self):
        p=self.opened();self.book();self.mark();self.poller.cycle()
        self.f.quote_connection('BTCUSDT','book',False,'disconnect')
        self.f.quote_connection('BTCUSDT','market',False,'disconnect')
        self.now=self.b.active()[1]['opened_ms']+p['hold_ms'];start=time.monotonic();self.poller.cycle()
        self.assertLess(time.monotonic()-start,1);state=self.b.active()[1]
        self.assertEqual(state['protection_trigger']['reason'],'TIME');self.assertEqual(state['protection_trigger']['observed_ms'],self.now)
        self.assertFalse(state['protection_trigger']['quote_confirmed']);self.assertEqual(self.b.snapshot()['fills'],1)
        trigger=state['protection_trigger'];self.b.close();self.now+=1000;self.b=Book(self.root,clock=lambda:self.now);self.poller.book=self.b
        for channel in ('book','market'):self.f.quote_connection('BTCUSDT',channel,True)
        self.book(101);self.mark();self.poller.cycle();state=self.b.plans()[0][1]
        self.assertEqual(state['last_fill_ms'],self.now);self.assertEqual(state['protection_trigger'],trigger)
        self.assertEqual(state['last_protective_execution']['reason'],'TIME');self.assertTrue(self.b.verify()['balanced'])
    def test_disconnect_blocks_even_a_fresh_cached_quote_at_deadline(self):
        p=self.opened();due=self.b.active()[1]['opened_ms']+p['hold_ms'];self.now=due-10
        old=self.book();self.mark();self.poller.cycle()
        self.f.quote_connection('BTCUSDT','book',False,'disconnect');self.now=due;self.poller.cycle()
        self.assertEqual(self.b.snapshot()['fills'],1)
        self.f.quote_connection('BTCUSDT','book',True);self.f.receive_quote('BTCUSDT',old,'book')
        self.now+=1;self.poller.cycle();self.assertEqual(self.b.snapshot()['fills'],2)
        self.assertEqual(self.b.plans()[0][1]['protection_trigger']['observed_ms'],due)
    def test_component_future_time_wrong_symbol_type_and_revisions_are_not_accepted(self):
        raw=self.book();self.mark(stamp=self.now+1)
        self.assertTrue(self.f.quote_packet('BTCUSDT')['error'])
        for patch in [{'s':'ETHUSDT'},{'st':2},{'st':True},{'e':'depthUpdate'}]:
            with self.assertRaises(ValueError):self.f.receive_quote('BTCUSDT',{**raw,**patch},'book')
        self.now+=10;self.mark();self.book(101);self.assertIsNone(self.f.quote_packet('BTCUSDT')['error'])
        self.f.receive_quote('BTCUSDT',{**self.f.quote_states['BTCUSDT']['book_last'],'b':'99'},'book')
        self.assertTrue(self.f.quote_packet('BTCUSDT')['error']);self.book(101)
        self.assertTrue(self.f.quote_packet('BTCUSDT')['error']);self.book(102)
        self.assertIsNone(self.f.quote_packet('BTCUSDT')['error'])
    def test_duplicate_and_mark_update_do_not_rejuvenate_source_or_book_identity(self):
        raw=self.book();self.mark();first=self.f.quote_packet('BTCUSDT')['quote'];self.now+=1000
        self.f.receive_quote('BTCUSDT',raw,'book');self.mark();second=self.f.quote_packet('BTCUSDT')['quote']
        self.assertEqual(first['liquidity_id'],second['liquidity_id']);self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(first['event_ms'],second['event_ms']);self.assertEqual(self.f.quote_states['BTCUSDT']['book']['received_ms'],T+100)
    def test_invalid_time_cannot_poison_the_next_valid_identity(self):
        self.book();self.mark();self.f.quote_packet('BTCUSDT')
        self.book(10**9,stamp=self.now+100000);self.assertTrue(self.f.quote_packet('BTCUSDT')['error'])
        self.assertEqual(self.f.quote_states['BTCUSDT']['book_last']['u'],100)
        self.now+=1;self.book(101);self.assertIsNone(self.f.quote_packet('BTCUSDT')['error'])
        self.now+=6001;self.book(10**9,stamp=self.now-6000);self.f.quote_packet('BTCUSDT')
        self.book(102);self.mark();self.assertIsNone(self.f.quote_packet('BTCUSDT')['error'])
    def test_quote_overflow_blocks_entry_without_filling_partial_position(self):
        self.opened();self.f.quote_capacity=2;self.mark();self.book(100);self.book(101);self.book(102)
        self.poller.cycle();self.assertEqual(self.b.snapshot()['fills'],1)
        self.assertEqual(self.b.active()[1]['remaining'],'0');self.assertGreater(self.f.quote_states['BTCUSDT']['overflows'],0)
        self.assertLessEqual(len(self.f.quote_states['BTCUSDT']['queue']),2);self.assertTrue(self.b.verify()['balanced'])
    def test_restart_regression_cannot_substitute_an_old_cached_execution_quote(self):
        self.opened();self.now+=100
        cached={**snapshot()[0]['quote'],'id':'durable-newer','event_ms':self.now,'received_ms':self.now}
        self.b.market('quote',cached)
        self.book(100,'104',stamp=self.now-10);self.mark('104',stamp=self.now-10);self.poller.cycle()
        self.assertEqual(self.b.snapshot()['fills'],1)
        self.assertEqual(self.b.get('quote_execution_unconfirmed:BTCUSDT'),'quote_source_regression')
        self.now+=1;self.book(101,'104');self.mark('104');self.poller.cycle()
        self.assertEqual(self.b.plans()[0][1]['open_qty'],'0');self.assertTrue(self.b.verify()['balanced'])
    def test_trade_id_gap_does_not_prevent_fresh_quote_stop(self):
        self.opened();self.book();self.mark();self.poller.cycle()
        self.f.receive('BTCUSDT',{'e':'aggTrade','s':'BTCUSDT','a':2,'T':self.now,'p':'100','q':'1','m':True})
        self.f.receive('BTCUSDT',{'e':'aggTrade','s':'BTCUSDT','a':4,'T':self.now,'p':'100','q':'1','m':True})
        self.now+=1;self.book(101,'104');self.mark('104');self.poller.cycle()
        self.assertEqual(self.b.plans()[0][1]['open_qty'],'0');self.assertTrue(self.b.verify()['balanced'])
    def test_routes_and_combined_frame_dispatch_have_no_rest_dependency(self):
        seen=[];now=self.now
        for channel in ('book','market'):
            self.f.stop.clear()
            raw=({'e':'bookTicker','s':'BTCUSDT','u':100,'E':now,'T':now,'b':'100','a':'100','B':'1','A':'1'} if channel=='book' else
                 {'stream':'btcusdt@markPrice@1s','data':{'e':'markPriceUpdate','s':'BTCUSDT','E':now,'p':'100','T':now+10000}})
            feed=self.f
            class Socket:
                def recv(self):feed.stop.set();return json.dumps(raw)
                def close(self):pass
            def connect(url):seen.append(url);return Socket()
            self.f.connect=connect;self.f.run('BTCUSDT',channel)
        self.assertTrue(any('/public/ws/btcusdt@bookTicker' in s for s in seen))
        self.assertTrue(any('/market/stream?streams=btcusdt@aggTrade/btcusdt@markPrice@1s' in s for s in seen))
        self.assertIsNone(self.f.quote_packet('BTCUSDT')['error'])


if __name__=='__main__':unittest.main()
