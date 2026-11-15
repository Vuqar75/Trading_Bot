"""Source recovery on temporary ledgers; no network, model or working orders."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.ai_limit_book import Book
from research.ai_limit_feed import Feed
from research.ai_limit_runtime import Poller,analyst
from tests.test_ai_limit_brackets import snapshot,T


class InlineExecutor:
    def submit(self,fn,*args,**kwargs):
        from concurrent.futures import Future
        result=Future()
        try:result.set_result(fn(*args,**kwargs))
        except BaseException as error:result.set_exception(error)
        return result
    def shutdown(self,**kwargs):pass


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
        self.feed.filters=lambda symbol,force=False:{**snapshot()[0]['filters'],'as_of_ms':self.now}
        self.book.set('filters:BTCUSDT',{**snapshot()[0]['filters'],'as_of_ms':self.now})
        self.poller=Poller(self.book,self.feed,symbols=('BTCUSDT',),trade_executor=InlineExecutor(),quote_executor=InlineExecutor())
        # Funding is independently covered by existing bracket tests. Isolate
        # source admission deterministically without a background worker.
        self.poller.funding_ready=lambda:None
    def tearDown(self):self.poller.close();self.book.close();self.temp.cleanup()
    def row(self,i,age=0,price='100',qty='1'):
        return {'a':i,'T':self.now-age,'p':price,'q':qty,'m':True}
    def accept(self):
        s,d=snapshot();self.book.accept(s,d,provenance={'kind':'fixture','response_id':'recovery','model':'fixture'})
    def full_page(self,start):
        return [self.row(i,0,'100') for i in range(start,start+1000)]

    def test_full_pages_drain_is_bounded_and_history_persists(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[self.full_page(10),self.full_page(1010),self.full_page(2010)]
        self.feed.weight_recent=(100,1000000000)
        with patch('research.ai_limit_runtime.time.monotonic_ns',return_value=1000000000):
            for i in range(3):
                result=self.poller.cycle()
                self.assertEqual(result['market_errors']['BTCUSDT'],'public_trade_backlog_unconfirmed')
                self.assertEqual(self.poller.sleep_after_cycle(),0 if i<2 else 2)
        pages=self.book.get('full_page_history:BTCUSDT')
        self.assertEqual([p['first_id'] for p in pages],[10,1010,2010])
        self.assertEqual([p['cursor_after'] for p in pages],[1009,2009,3009])
        self.assertEqual(pages[0]['requested_after_id'],9)
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),3009)
        self.assertTrue(self.book.verify()['balanced'])

    def test_full_page_history_is_bounded_and_durable(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[self.full_page(10+1000*i) for i in range(13)]
        for _ in range(13):self.poller.cycle();self.now+=1
        pages=self.book.get('full_page_history:BTCUSDT')
        self.assertEqual(len(pages),12);self.assertEqual(pages[0]['first_id'],1010)
        self.assertEqual(pages[-1]['consecutive_full_pages'],13)
        self.assertEqual(pages[-1]['processing'],'committed')
        self.assertTrue(self.book.verify()['balanced'])
        path=self.book.root;self.book.close()
        self.book=Book(path,clock=lambda:self.now)
        self.assertEqual(self.book.get('full_page_history:BTCUSDT'),pages)
        self.assertTrue(self.book.verify()['balanced'])

    def test_slow_page_and_elapsed_drain_budget_force_normal_pause(self):
        import time
        old=self.feed.fetch
        def slow(path,params):
            time.sleep(.03);return old(path,params)
        self.feed.fetch=slow;self.book.set('trade_cursor:BTCUSDT',9);self.batches=[self.full_page(10)]
        self.poller.cycle();page=self.book.get('full_page_history:BTCUSDT')[-1]
        self.assertGreaterEqual(page['fetch_monotonic_ms'],20)
        self.feed.weight_recent=(100,1000000000)
        with patch('research.ai_limit_runtime.time.monotonic_ns',side_effect=[1000000000,2100000001]):
            self.assertEqual(self.poller.sleep_after_cycle(),0)
            self.assertEqual(self.poller.sleep_after_cycle(),2)
        self.assertTrue(self.book.verify()['balanced'])

    def test_acceleration_requires_recent_low_shared_ip_weight(self):
        self.poller.full_page_pending=True
        with patch('research.ai_limit_runtime.time.monotonic_ns',return_value=10000000000):
            self.feed.weight_recent=None;self.assertEqual(self.poller.sleep_after_cycle(),2)
            self.feed.weight_recent=(1000,10000000000);self.assertEqual(self.poller.sleep_after_cycle(),2)
            self.feed.weight_recent=(20,4000000000);self.assertEqual(self.poller.sleep_after_cycle(),2)
            self.feed.weight_recent=(20,10000000000);self.assertEqual(self.poller.sleep_after_cycle(),0)

    def test_full_page_cancels_pending_entry_without_fill(self):
        self.accept();self.book.set('trade_cursor:BTCUSDT',9)
        self.batches=[self.full_page(10)]
        result=self.poller.cycle();state=self.book.plans()[0][1]
        self.assertEqual(result['fills'],0);self.assertEqual(state['status'],'CANCELED')
        self.assertEqual(state['entry_cancellation']['detail'],'trade_backlog_unconfirmed')
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_trade_backlog_unconfirmed')
        self.assertTrue(self.book.verify()['balanced'])

    def test_full_page_partial_position_keeps_protection_and_no_accelerated_drain(self):
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-full','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[self.full_page(10)]
        self.poller.cycle();state=self.book.active()[1]
        self.assertEqual(state['remaining'],'0');self.assertEqual(state['open_qty'],'0.2')
        self.assertEqual(self.poller.sleep_after_cycle(),2)
        self.now+=100
        q={**snapshot()[0]['quote'],'id':'full-stop','event_ms':self.now,'received_ms':self.now,'bid':'94','ask':'94.1','mark':'94'}
        self.book.market('quote',q);self.assertEqual(self.book.active()[1]['open_qty'],'0')
        self.assertTrue(self.book.verify()['balanced'])

    def test_full_page_survives_processing_error_without_cursor_advance(self):
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-page-error','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[self.full_page(10)]
        original=self.book.market
        def fail_trade(kind,event):
            if kind=='trade':raise ValueError('simulated_page_failure')
            return original(kind,event)
        with patch.object(self.book,'market',side_effect=fail_trade):result=self.poller.cycle()
        page=self.book.get('full_page_history:BTCUSDT')[-1]
        self.assertEqual(page['processing'],'pending');self.assertEqual(page['first_id'],10)
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),9)
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_market_unconfirmed')
        self.assertEqual(self.book.active()[1]['open_qty'],'0.2')
        self.assertTrue(self.book.verify()['balanced'])

    def test_repeated_or_missing_page_id_is_still_a_gap(self):
        self.book.set('trade_cursor:BTCUSDT',1009)
        self.batches=[[self.row(1009),self.row(1010)],[self.row(5000)]]
        result=self.poller.cycle()
        self.assertTrue(result['last_trade_gap']['source']['discontinuities'])
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_trade_stream_unconfirmed')
        self.assertEqual(result['fills'],0)

    def test_slow_trade_http_does_not_hold_next_quote_protection(self):
        import threading,time
        from concurrent.futures import ThreadPoolExecutor
        self.poller.trade_pool=ThreadPoolExecutor(max_workers=1)
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-slow-http','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('trade_cursor:BTCUSDT',9);quotes=[];gate=threading.Event();started=threading.Event()
        def quote(symbol):
            quotes.append(time.monotonic());stop=len(quotes)>1
            q={**snapshot()[0]['quote'],'id':'slow-q-'+str(len(quotes)),'event_ms':self.now,'received_ms':self.now}
            if stop:q.update(bid='94',ask='94.1',mark='94')
            return q,self.now+100000
        def slow(path,params):
            started.set();gate.wait(2)
            return [{'a':10,'T':self.now,'p':'100','q':'1','m':True}]
        self.feed.book=quote;self.feed.fetch=slow
        try:
            begin=time.monotonic();self.poller.cycle();self.assertLess(time.monotonic()-begin,.5)
            self.assertTrue(started.wait(1));self.assertEqual(self.book.active()[1]['open_qty'],'0.2')
            self.now+=100;self.poller.cycle()
            self.assertEqual(len(quotes),2);self.assertEqual(self.book.active()[1]['open_qty'],'0')
            self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_slow_quote_waits_safely_then_protects_on_fresh_quote(self):
        import threading,time
        from concurrent.futures import ThreadPoolExecutor
        self.poller.quote_pool=ThreadPoolExecutor(max_workers=1)
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-slow-quote','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.now+=6000;gate=threading.Event();started=threading.Event()
        def slow(symbol):
            started.set();gate.wait(2)
            q={**snapshot()[0]['quote'],'id':'new-stop','event_ms':self.now,'received_ms':self.now,'bid':'94','ask':'94.1','mark':'94'}
            return q,self.now+100000
        self.feed.book=slow
        try:
            begin=time.monotonic();result=self.poller.cycle();self.assertLess(time.monotonic()-begin,.5)
            self.assertTrue(started.wait(1));state=self.book.active()[1]
            self.assertEqual(state['open_qty'],'0.2');self.assertEqual(state['remaining'],'0')
            self.assertEqual(result['market_errors']['BTCUSDT'],'public_market_unconfirmed')
            self.assertEqual(self.book.db.execute('SELECT count(*) FROM bracket_fills').fetchone()[0],1)
            gate.set();self.poller.quote_jobs['BTCUSDT'][0].result(timeout=1)
            self.now+=100;self.batches=[[]];self.poller.cycle()
            self.assertEqual(self.book.active()[1]['open_qty'],'0');self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_restart_after_pending_trade_request_keeps_position_and_cursor(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.poller.trade_pool=ThreadPoolExecutor(max_workers=1)
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-restart-http','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('trade_cursor:BTCUSDT',9);gate=threading.Event();started=threading.Event()
        def slow(path,params):
            started.set();gate.wait(2)
            return [{'a':10,'T':self.now,'p':'100','q':'1','m':True}]
        self.feed.fetch=slow
        try:
            self.poller.cycle();self.assertTrue(started.wait(1))
            gate.set();self.poller.close();path=self.book.root;self.book.close()
            self.book=Book(path,clock=lambda:self.now)
            self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),9)
            self.assertEqual(self.book.active()[1]['open_qty'],'0.2')
            self.assertEqual(self.book.active()[1]['remaining'],'0')
            self.assertEqual(self.book.db.execute('SELECT count(*) FROM bracket_fills').fetchone()[0],1)
            self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_stale_quote_wait_preserves_position_and_time_exit_state(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.poller.quote_pool=ThreadPoolExecutor(max_workers=1)
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-time-http','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.now+=240*60000+100;gate=threading.Event();started=threading.Event()
        def slow(symbol):
            started.set();gate.wait(2)
            q={**snapshot()[0]['quote'],'id':'time-fresh','event_ms':self.now,'received_ms':self.now}
            return q,self.now+100000
        self.feed.book=slow
        try:
            self.poller.cycle();self.assertTrue(started.wait(1))
            state=self.book.active()[1]
            self.assertEqual(state['status'],'EXIT_PENDING');self.assertEqual(state['close_reason'],'TIME')
            self.assertEqual(state['open_qty'],'0.2')
            self.assertEqual(self.book.db.execute('SELECT count(*) FROM bracket_fills').fetchone()[0],1)
            gate.set();self.poller.quote_jobs['BTCUSDT'][0].result(timeout=1)
            self.now+=100;self.batches=[[]];self.poller.cycle()
            self.assertEqual(self.book.active()[1]['open_qty'],'0');self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_slow_trade_result_is_rechecked_and_not_filled_late(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.poller.trade_pool=ThreadPoolExecutor(max_workers=1)
        self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-late-http','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('trade_cursor:BTCUSDT',9);gate=threading.Event();started=threading.Event()
        stamp=self.now
        def slow(path,params):
            started.set();gate.wait(2)
            return [{'a':10,'T':stamp,'p':'98.9','q':'1','m':True}]
        self.feed.fetch=slow
        try:
            self.poller.cycle();self.assertTrue(started.wait(1))
            self.now+=6000;self.poller.cycle()
            self.assertEqual(self.book.active()[1]['remaining'],'0')
            self.assertEqual(self.book.active()[1]['open_qty'],'0.2')
            gate.set();self.poller.trade_jobs['BTCUSDT'][0].result(timeout=1)
            result=self.poller.cycle()
            self.assertIsNotNone(result['market_errors']['BTCUSDT'])
            self.assertEqual(self.book.active()[1]['open_qty'],'0.2')
            self.assertEqual(self.book.db.execute('SELECT count(*) FROM bracket_fills').fetchone()[0],1)
            self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_poll_error_diagnostics_are_bounded_and_redact_unknown_text(self):
        def failing(symbol):raise ValueError('sensitive arbitrary text')
        self.feed.book=failing
        for _ in range(10):self.poller.cycle();self.now+=1
        history=self.book.get('poll_error_history:BTCUSDT')
        self.assertEqual(len(history),8)
        self.assertTrue(all(x['stage']=='quote_request' and x['reason']=='other' and x['class']=='ValueError' for x in history))
        self.assertNotIn('sensitive',str(history))
        self.assertEqual(self.book.get('market_error:BTCUSDT'),'public_market_unconfirmed')
        self.assertTrue(self.book.verify()['balanced'])

    def test_poll_error_diagnostics_record_trade_failure_and_preserve_cursor(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        def failing(path,params):raise ValueError('public_market_unavailable')
        self.feed.fetch=failing;result=self.poller.cycle()
        event=self.book.get('poll_error_history:BTCUSDT')[-1]
        self.assertEqual((event['stage'],event['reason']),('trade_request','public_market_unavailable'))
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),9)
        self.assertEqual(result['fills'],0)
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_market_unconfirmed')
        self.assertTrue(self.book.verify()['balanced'])

    def test_http_timing_records_latency_and_parse_without_payload(self):
        import time
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self,limit):
                time.sleep(.025)
                return b'{"serverTime":123}'
        with patch('research.ai_limit_feed.urllib.request.urlopen',return_value=Response()):
            self.assertEqual(self.feed.get('/fapi/v1/time',{}),{'serverTime':123})
        row=self.feed.http_timings[-1]
        self.assertGreaterEqual(row['http_ms'],20)
        self.assertIn('parse_ms',row)
        self.assertNotIn('serverTime',str(row))
        self.assertEqual(row['outcome'],'ok')

    def test_gap_records_bounded_monotonic_phase_history(self):
        self.book.set('trade_cursor:BTCUSDT',9)
        self.poller.phase_history.append({'started_ns':123,'sleep_ms':2001,'publish_ms':4})
        self.feed.http_timings.append({'path':'/fapi/v1/ticker/bookTicker','http_ms':5,'parse_ms':1,'started_ns':123,'ended_ns':456})
        self.batches=[[self.row(10,6000)],[self.row(100)]]
        result=self.poller.cycle();diag=self.book.get('last_gap_phase_diagnostics')['diagnostics']
        self.assertNotIn('phase_diagnostics',result['last_trade_gap']['source'])
        self.assertEqual(diag['clock'],'monotonic_ns')
        self.assertEqual(diag['prior_cycles'][0]['sleep_ms'],2001)
        self.assertEqual(diag['http_recent'][0]['http_ms'],5)
        self.assertIn('filters_refresh_ms',diag['phases_ms'])
        self.assertEqual(self.book.balance('reserved'),0)
        self.assertEqual(result['fills'],0)

    def test_diagnostic_storage_is_single_bounded_record(self):
        self.book.trade_gap('BTCUSDT','first',evidence={'gap':True},phase_evidence={'cycle':1})
        self.now+=1
        self.book.trade_gap('BTCUSDT','second',evidence={'gap':True},phase_evidence={'cycle':2})
        self.assertEqual(self.book.get('last_gap_phase_diagnostics')['diagnostics'],{'cycle':2})
        count=self.book.db.execute("SELECT count(*) FROM bracket_meta WHERE key='last_gap_phase_diagnostics'").fetchone()[0]
        self.assertEqual(count,1)
        rows=self.book.db.execute('SELECT payload FROM bracket_messages').fetchall()
        self.assertFalse(any('phase_evidence' in row[0] or '"cycle"' in row[0] for row in rows))

    def test_slow_filter_refresh_does_not_block_trade_poll(self):
        import threading,time
        gate=threading.Event();started=threading.Event()
        def slow(symbol,force=False):
            started.set();gate.wait(2)
            return {**snapshot()[0]['filters'],'as_of_ms':self.now}
        self.feed.filters=slow
        self.book.set('filters:BTCUSDT',{**snapshot()[0]['filters'],'as_of_ms':self.now-51000})
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[[self.row(10)]]
        try:
            begin=time.monotonic();result=self.poller.cycle();elapsed=time.monotonic()-begin
            self.assertTrue(started.wait(1));self.assertLess(elapsed,.5)
            self.assertTrue(result['trade_feeds']['BTCUSDT']['phase_diagnostics']['phases_ms']['filters_ready'])
            self.assertFalse(result['trade_feeds']['BTCUSDT']['execution_gap'])
            self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),10)
        finally:gate.set()

    def test_failed_refresh_blocks_entry_and_retains_protection(self):
        import threading
        gate=threading.Event();started=threading.Event()
        def failing(symbol,force=False):
            started.set();gate.wait(2)
            raise ValueError('public_market_unavailable')
        self.feed.filters=failing;self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-failed-refresh','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('filters:BTCUSDT',{**snapshot()[0]['filters'],'as_of_ms':self.now-51000})
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[[self.row(10)]]
        try:
            self.poller.cycle();self.assertTrue(started.wait(1));gate.set()
            try:self.poller.filter_jobs['BTCUSDT'].result(timeout=1)
            except ValueError:pass
            self.now+=100;self.batches=[[self.row(11)]];result=self.poller.cycle()
            state=self.book.active()[1]
            self.assertEqual(state['remaining'],'0');self.assertEqual(state['open_qty'],'0.2')
            self.assertEqual(result['market_errors']['BTCUSDT'],'public_filters_unconfirmed')
            self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_completed_changed_filters_cancel_entry_before_following_trade(self):
        import threading
        gate=threading.Event();started=threading.Event()
        def slow(symbol,force=False):
            started.set();gate.wait(2)
            return {**snapshot()[0]['filters'],'as_of_ms':self.now,'tick_size':'0.02'}
        self.feed.filters=slow;self.accept()
        self.book.set('filters:BTCUSDT',{**snapshot()[0]['filters'],'as_of_ms':self.now-51000})
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[[self.row(10)]]
        try:
            self.poller.cycle();self.assertTrue(started.wait(1));gate.set()
            self.poller.filter_jobs['BTCUSDT'].result(timeout=1)
            self.now+=100;self.batches=[[self.row(11,0,'98.9')]]
            result=self.poller.cycle()
            self.assertEqual(result['fills'],0)
            self.assertEqual(self.book.plans()[0][1]['entry_cancellation']['reason'],'FILTERS_CHANGED')
            self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

    def test_filter_deadline_cancels_entry_but_keeps_partial_position_protected(self):
        import threading
        gate=threading.Event();started=threading.Event()
        def slow(symbol,force=False):
            started.set();gate.wait(2)
            return {**snapshot()[0]['filters'],'as_of_ms':self.now}
        self.feed.filters=slow;self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-filter','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.book.set('filters:BTCUSDT',{**snapshot()[0]['filters'],'as_of_ms':self.now-60001})
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[[self.row(10)]]
        try:
            result=self.poller.cycle();self.assertTrue(started.wait(1))
            state=self.book.active()[1]
            self.assertEqual(state['remaining'],'0');self.assertEqual(state['open_qty'],'0.2')
            self.assertEqual(state['entry_cancellation']['reason'],'ENTRY_EVIDENCE_STALE')
            self.assertEqual(result['market_errors']['BTCUSDT'],'public_filters_unconfirmed')
            self.now+=100
            q={**snapshot()[0]['quote'],'id':'filter-stop','event_ms':self.now,'received_ms':self.now,'bid':'94','ask':'94.1','mark':'94'}
            self.book.market('quote',q)
            self.assertEqual(self.book.active()[1]['open_qty'],'0');self.assertTrue(self.book.verify()['balanced'])
        finally:gate.set()

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
        self.accept();self.book.set('trade_cursor:BTCUSDT',9);self.now+=7000
        self.batches=[[self.row(10,6000,'98.9')],[self.row(100)]]
        result=self.poller.cycle()
        self.assertEqual(result['fills'],0);self.assertIsNone(self.book.active())
        self.assertEqual(self.book.plans()[0][1]['status'],'CANCELED')
        self.assertIsNotNone(result['market_errors']['BTCUSDT'])
        self.assertEqual(self.book.balance('reserved'),0)
    def test_contiguous_stale_prefix_before_arming_is_not_a_fill_or_gap(self):
        self.accept();self.book.set('trade_cursor:BTCUSDT',9);armed=self.book.active()[1]['armed_ms'];self.now+=6100
        self.batches=[[self.row(10,6100,'98.9'),self.row(11,0,'100')]]
        result=self.poller.cycle();report=result['trade_feeds']['BTCUSDT']
        self.assertEqual(report['stale_last_ms'],armed)
        self.assertEqual(report['ignored_prearm_stale_rows'],1)
        self.assertTrue(report['history_contiguous']);self.assertFalse(report['execution_gap'])
        self.assertEqual(result['fills'],0);self.assertIsNone(result['market_errors']['BTCUSDT'])
        self.assertEqual(self.book.active()[1]['status'],'WORKING')
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),11)
    def test_prearm_stale_with_missing_id_still_cancels(self):
        self.accept();self.book.set('trade_cursor:BTCUSDT',9);self.now+=6100
        self.batches=[[self.row(12,6100,'98.9')],[self.row(100)]]
        result=self.poller.cycle()
        self.assertEqual(result['fills'],0);self.assertIsNone(self.book.active())
        self.assertEqual(self.book.plans()[0][1]['entry_cancellation']['reason'],'TRADE_GAP')
        self.assertEqual(result['last_trade_gap']['source']['discontinuities'],[{'expected':10,'received':12}])
    def test_prearm_stale_without_persisted_cursor_cannot_prove_continuity(self):
        self.accept();self.now+=6100
        self.batches=[[self.row(12,6100,'98.9')],[self.row(100)]]
        result=self.poller.cycle()
        self.assertEqual(result['fills'],0);self.assertIsNone(self.book.active())
        self.assertFalse(result['last_trade_gap']['source']['history_contiguous'])
        self.assertEqual(self.book.plans()[0][1]['entry_cancellation']['reason'],'TRADE_GAP')
    def test_idle_batch_is_skipped_atomically_and_cursor_survives_restart(self):
        self.book.set('trade_cursor:BTCUSDT',9);self.batches=[[self.row(i) for i in range(10,410)]]
        with patch.object(self.book,'market',wraps=self.book.market) as market:
            result=self.poller.cycle()
        self.assertEqual([call.args[0] for call in market.call_args_list],['quote'])
        self.assertEqual(result['trade_feeds']['BTCUSDT']['idle_events_skipped'],400)
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),409)
        self.assertEqual(self.book.db.execute('select count(*) from bracket_inputs').fetchone()[0],0)
        self.book.close();self.book=Book(Path(self.temp.name)/'paper',clock=lambda:self.now);self.poller.book=self.book
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),409)
        self.batches=[[self.row(410)]];result=self.poller.cycle()
        self.assertIn('batch_finalize_ms',result['trade_feeds']['BTCUSDT']['previous_cycle'])
        self.assertEqual(self.requests[-1]['fromId'],410);self.assertEqual(self.book.snapshot()['fills'],0)
    def test_active_batch_failure_rolls_back_fill_and_cursor_before_retry(self):
        self.accept();self.book.set('trade_cursor:BTCUSDT',9);self.now+=100
        rows=[self.row(10,0,'98.9','.2'),self.row(11,0,'100','.2')];self.batches=[rows]
        real=self.book.market
        def fail_second(kind,event):
            if kind=='trade' and event['id']=='11':raise ValueError('injected_batch_failure')
            return real(kind,event)
        with patch.object(self.book,'market',side_effect=fail_second):result=self.poller.cycle()
        self.assertEqual(result['fills'],0);self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),9)
        self.assertEqual(result['market_errors']['BTCUSDT'],'public_market_unconfirmed')
        self.assertEqual(self.book.active()[1]['open_qty'],'0');self.assertTrue(self.book.verify()['balanced'])
        self.batches=[rows];self.poller.cycle()
        self.assertEqual(self.book.snapshot()['fills'],1);self.assertEqual(self.book.active()[1]['open_qty'],'0.2')
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),11);self.assertTrue(self.book.verify()['balanced'])
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
        self.assertEqual(result['blocking_reasons'],['public_trade_stream_unconfirmed'])
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
