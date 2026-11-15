import unittest,time,threading
from research.ai_limit_stream import StreamFeed
from tests.test_ai_limit_trade_recovery import TradeRecoveryTests,InlineExecutor
from research.ai_limit_runtime import Poller

class StreamTests(TradeRecoveryTests):
    # Inherited existing regressions run unchanged too.
    def stream(self,cap=32768):
        f=StreamFeed(symbols=['BTCUSDT'],capacity=cap,clock=lambda:self.now)
        f.book=self.feed.book;f.filters=self.feed.filters
        f.states['BTCUSDT']['connected']=True
        self.poller.close();self.feed=f
        self.poller=Poller(self.book,f,symbols=['BTCUSDT'],trade_executor=InlineExecutor(),quote_executor=InlineExecutor())
        self.poller.funding_ready=lambda:None
        return f
    def send(self,f,i,age=0):
        f.receive('BTCUSDT',{'e':'aggTrade','s':'BTCUSDT','a':i,'T':self.now-age,'p':'100','q':'1','m':True})
    def bootstrap(self,f):
        self.send(f,1);f.trades('BTCUSDT');self.book.set('trade_cursor:BTCUSDT',1)
    def test_burst_delivery_not_exchange_timestamp_rate(self):
        f=self.stream();self.bootstrap(f);self.accept()
        # 10k delivered over 10 batches at 10ms intervals; event timestamps
        # cover 31ms independently. Consumer and producer run concurrently.
        start=time.monotonic();counts=[]
        def producer():
            for block in range(10):
                for j in range(1000):self.send(f,2+block*1000+j,31-((block*1000+j)*31//9999))
                time.sleep(.01)
        t=threading.Thread(target=producer);t.start()
        while t.is_alive() or f.states['BTCUSDT']['queue']:
            self.poller.cycle();counts.append(len(f.states['BTCUSDT']['queue']))
        t.join();elapsed=time.monotonic()-start
        self.assertEqual(self.book.get('trade_cursor:BTCUSDT'),10001)
        self.assertLess(elapsed,5);self.assertTrue(self.book.verify()['balanced'])
        print('BURST',{'events':10000,'elapsed_s':elapsed,'highwater':f.states['BTCUSDT']['highwater'],'remaining':counts[-1]})
    def test_gap_disconnect_overflow_duplicate_and_idle_recovery(self):
        f=self.stream(cap=4);self.bootstrap(f);self.send(f,2);self.send(f,2)
        self.assertEqual(f.states['BTCUSDT']['duplicates'],1)
        self.send(f,4);self.assertTrue(f.trades('BTCUSDT',1)[2])
        self.assertTrue(f.trades('BTCUSDT')[2])
        self.send(f,5);self.assertFalse(f.trades('BTCUSDT',4)[2])
        f.fail('BTCUSDT','disconnect');self.assertTrue(f.trades('BTCUSDT',5)[2])
        f.states['BTCUSDT']['connected']=True
        for i in range(6,12):self.send(f,i)
        self.assertLessEqual(len(f.states['BTCUSDT']['queue']),4)
        self.assertTrue(f.trades('BTCUSDT',5)[2])
    def test_partial_position_stale_stream_cannot_fill_and_quote_protects(self):
        f=self.stream();self.bootstrap(f);self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-stream','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        self.now+=6001;self.send(f,2,6000);self.poller.cycle()
        self.assertEqual(self.book.plans()[0][1]['remaining'],'0')
        self.assertTrue(self.book.verify()['balanced'])
    def test_stream_burst_partial_position_quote_stop_has_priority(self):
        from tests.test_ai_limit_brackets import snapshot
        f=self.stream();self.bootstrap(f);self.accept();self.now+=100
        self.book.market('trade',{'id':'partial-burst','symbol':'BTCUSDT','event_ms':self.now,'received_ms':self.now,'price':'98.9','qty':'.2','buyer_maker':True})
        for i in range(2,10002):self.send(f,i)
        self.now+=100
        f.book=lambda symbol:({**snapshot()[0]['quote'],'id':'burst-stop','event_ms':self.now,'received_ms':self.now,'bid':'94','ask':'94.1','mark':'94'},self.now+100000)
        start=time.monotonic();self.poller.cycle()
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(self.book.plans()[0][1]['open_qty'],'0')
        self.assertTrue(self.book.verify()['balanced'])
    def test_stream_slow_delivery_never_backfills_stale_rows(self):
        f=self.stream();self.bootstrap(f);self.accept();self.now+=10000
        self.send(f,2,6000)
        self.poller.cycle()
        self.assertEqual(self.book.snapshot()['fills'],0)
        self.assertEqual(self.book.plans()[0][1]['remaining'],'0')
    def test_transport_reconnect_is_latched_until_idle_bootstrap(self):
        f=self.stream();self.bootstrap(f)
        f.fail('BTCUSDT','disconnect');f.states['BTCUSDT']['connected']=True
        self.send(f,2)
        self.assertTrue(f.trades('BTCUSDT',1)[2])
        f.trades('BTCUSDT');self.send(f,3)
        self.assertFalse(f.trades('BTCUSDT',2)[2])

    def test_older_quote_cannot_replace_or_rejuvenate_current_quote(self):
        from tests.test_ai_limit_brackets import snapshot
        f=self.stream();self.bootstrap(f);self.poller.cycle()
        original=self.book.get('quote:BTCUSDT');self.now+=100
        f.book=lambda symbol:({**original,'id':'older','event_ms':original['event_ms']-1,'received_ms':self.now},self.now+100000)
        self.poller.cycle()
        self.assertEqual(self.book.get('quote:BTCUSDT'),original)
        self.assertEqual(self.book.get('last_quote_regression:BTCUSDT')['discarded_ms'],original['event_ms']-1)
