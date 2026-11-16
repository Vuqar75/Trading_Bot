"""Temporary paper ledgers; deterministic clocks/futures, no network or LLM."""
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from research.ai_limit_book import Book
from research.ai_limit_feed import Feed
from research.ai_limit_runtime import Poller
from tests.test_ai_limit_brackets import T, snapshot


class ProtectionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=T
        self.root=Path(self.temp.name)/'paper';self.b=Book(self.root,clock=lambda:self.now)
        self.b.funding(T,T,[]);self.b.pause(False)
    def tearDown(self):self.b.close();self.temp.cleanup()
    def opened(self,partial=False):
        s,d=snapshot('SHORT');self.b.accept(s,d,provenance={'kind':'fixture','response_id':'protection','model':'fixture'})
        p=self.b.active()[0];self.now+=100
        self.b.market('trade',{'id':'entry','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,
                              'price':'101.1','qty':'.2' if partial else p['quantity'],'buyer_maker':False})
        return p
    def quote(self,ident='fresh',price='100',qty='2'):
        return {**snapshot()[0]['quote'],'id':ident,'event_ms':self.now,'received_ms':self.now,
                'bid':price,'ask':price,'mark':price,'bid_qty':qty,'ask_qty':qty}
    def test_time_trigger_survives_unconfirmed_quote_and_restart_without_old_execution(self):
        p=self.opened();self.now=self.b.active()[1]['opened_ms']+p['hold_ms']
        self.b.pulse();s=self.b.active()[1]
        self.assertEqual(s['protection_trigger']['reason'],'TIME')
        self.assertEqual(s['protection_trigger']['observed_ms'],self.now)
        self.assertFalse(s['protection_trigger']['quote_confirmed']);self.assertEqual(self.b.snapshot()['fills'],1)
        triggered=s['protection_trigger'];self.b.close();self.now+=1000;self.b=Book(self.root,clock=lambda:self.now)
        self.b.market('quote',self.quote());s=self.b.plans()[0][1]
        self.assertEqual(s['protection_trigger'],triggered)
        self.assertEqual(s['last_protective_execution']['observed_ms'],self.now)
        self.assertEqual(s['last_fill_ms'],self.now);self.assertEqual(s['open_qty'],'0')
        self.assertEqual(s['last_protective_execution']['price'],'100.1')
        self.b.pulse();self.assertEqual(self.b.snapshot()['fills'],2);self.assertTrue(self.b.verify()['balanced'])
    def test_prior_quote_gap_does_not_change_later_time_price_or_freshness(self):
        p=self.opened();self.now+=6000;self.b.pulse();self.now+=2000
        self.b.market('quote',self.quote('recovered'))
        due=self.b.active()[1]['opened_ms']+p['hold_ms'];self.now=due-1000
        self.b.market('quote',self.quote('time-source','99'))
        self.now=due;self.b.pulse();s=self.b.plans()[0][1]
        self.assertEqual(s['last_fill_ms'],due);self.assertEqual(s['last_protective_execution']['price'],'99.099')
        self.assertEqual(s['last_protective_execution']['quote_age_ms'],1000)
        self.assertEqual(s['protection_trigger']['observed_ms'],due)
        self.assertEqual(len(s['protection_wait_history']),1);self.assertTrue(self.b.verify()['balanced'])
    def test_partial_stop_and_repeated_pulse_do_not_double_post(self):
        self.opened(partial=True);self.now+=100
        self.b.market('quote',self.quote('stop','104','.1'));s=self.b.active()[1]
        self.assertEqual(s['remaining'],'0');self.assertEqual(s['open_qty'],'0.1')
        first=s['protection_trigger'];count=self.b.snapshot()['fills'];self.b.pulse()
        self.assertEqual(self.b.snapshot()['fills'],count)
        self.now+=100;self.b.market('quote',self.quote('stop2','105','.1'))
        s=self.b.plans()[0][1];self.assertEqual(s['open_qty'],'0');self.assertEqual(s['protection_trigger'],first)
        self.assertEqual(s['last_protective_execution']['reason'],'SL');self.assertTrue(self.b.verify()['balanced'])
    def test_unconfirmed_intervals_are_bounded(self):
        self.opened()
        self.b.protection_phase_context={'cycle_started_ns':123,'prior_cycles':[{'phase_'+str(i):1000 for i in range(25)}]*2}
        for i in range(7):
            self.now+=6000;self.b.pulse();self.now+=1;self.b.market('quote',self.quote(str(i)))
        s=self.b.active()[1];self.assertEqual(len(s['protection_wait_history']),4)
        self.assertNotIn('phase_context',s['protection_wait_history'][0])
        self.assertEqual(len(self.b.get('protection_phase_history')),8)
        self.now=s['opened_ms']+self.b.active()[0]['hold_ms'];self.b.pulse()
        self.now+=1;self.b.market('quote',self.quote('exit'))
        public=self.b.snapshot();public['plans']=public['plans']*30
        self.assertLess(len(json.dumps(public)),200000);self.assertTrue(self.b.verify()['balanced'])
    def test_completed_failure_survives_next_pending_and_is_frozen_at_gap(self):
        self.opened();feed=Feed(clock=lambda:self.now);feed.streaming=True
        feed.quote_report={'BTCUSDT':{'phase':'validation','book_source_ms':self.now-6000,
                                     'mark_source_ms':self.now,'time_rejections':['book_stale']}}
        pending=Future()
        class PendingExecutor:
            def submit(self,*a,**k):return pending
            def shutdown(self,**k):pass
        poller=Poller(self.b,feed,symbols=['BTCUSDT'],quote_executor=PendingExecutor())
        poller.funding_ready=lambda:None;poller.filters_ready=lambda symbol:True
        try:
            for i in range(10):
                failed=Future();failed.set_exception(ValueError('stale_or_future_market_data'))
                poller.quote_jobs['BTCUSDT']=(failed,time.monotonic_ns());poller.cycle();self.now+=1
            first=self.b.get('quote_failure_history:BTCUSDT')
            self.assertEqual(len(first),8);self.assertEqual(first[-1]['quote_components']['time_rejections'],['book_stale'])
            self.now+=6000;feed.quote_report['BTCUSDT']={'phase':'mark_http'};poller.cycle()
            self.assertEqual(self.b.get('last_quote_unconfirmed:BTCUSDT')['kind'],'pending_http')
            self.assertEqual(self.b.get('quote_failure_history:BTCUSDT'),first)
            feed.trade_report={'BTCUSDT':{'rows':1,'stale_rows':1,'stale_first_ms':self.now-6000,
                                         'stale_last_ms':self.now-6000,'requested_after_id':1,'discontinuities':[]}}
            feed.trades=lambda *a:([],2,True)
            pending.set_result((self.quote('recovered'),self.now+100000));poller.cycle()
            frozen=self.b.get('last_gap_phase_diagnostics')['diagnostics']['quote_failures']
            self.assertEqual(frozen,first)
            self.b.set('quote_failure_history:BTCUSDT',[]);self.assertEqual(self.b.get('last_gap_phase_diagnostics')['diagnostics']['quote_failures'],first)
            self.assertEqual(self.b.snapshot()['fills'],1);self.assertTrue(self.b.verify()['balanced'])
        finally:poller.close()
    def test_http_error_type_and_duration_exclude_private_exception_text(self):
        feed=Feed(clock=lambda:T)
        error=urllib.error.URLError(TimeoutError('private arbitrary text'))
        with patch('urllib.request.urlopen',side_effect=error),patch('research.ai_limit_feed.time.monotonic_ns',side_effect=[1000000000,3000000000]):
            with self.assertRaisesRegex(ValueError,'public_market_unavailable'):feed.get('/fapi/v1/premiumIndex',{'symbol':'BTCUSDT'})
        row=feed.http_timings[-1];self.assertEqual(row['http_ms'],2000)
        self.assertEqual(row['exception_class'],'URLError');self.assertEqual(row['reason_class'],'TimeoutError')
        self.assertNotIn('private',json.dumps(row))


if __name__=='__main__':unittest.main()
