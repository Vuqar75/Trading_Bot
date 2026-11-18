"""Saved receive-after-old-check-time race, temporary paper fixtures only."""
import threading
from tests.test_ai_quote_stream import QuoteStreamTests

class QuotePacketRaceTests(QuoteStreamTests):
 def before_packet_lock(self,update):
  feed=self.f;actual=feed.lock
  class Interleave:
   def __enter__(hook):
    feed.lock=actual
    update()
    actual.acquire()
   def __exit__(hook,*args):actual.release()
  feed.lock=Interleave()
 def test_new_receipt_during_lock_wait_is_not_in_the_future(self):
  self.book();self.mark();previous=self.now
  def update():
   self.now+=1;self.book(101,stamp=previous)
  self.before_packet_lock(update)
  packet=self.f.quote_packet('BTCUSDT')
  self.assertIsNone(packet['error'],packet['report']['time_rejections'])
  self.assertEqual(packet['report']['received_ms'],self.now)
  self.assertLessEqual(packet['report']['components']['book']['received_ms'],packet['report']['received_ms'])
 def test_partial_position_SL_survives_receipt_interleaving(self):
  self.opened(qty='.2');self.now+=1;previous=self.now
  def update():
   self.now+=1;self.book(101,'104',stamp=previous);self.mark('104',stamp=previous)
  self.before_packet_lock(update);self.poller.cycle()
  state=self.b.plans()[0][1]
  self.assertEqual(state['open_qty'],'0');self.assertEqual(state['close_reason'],'SL')
  self.assertEqual(self.b.snapshot()['fills'],2);self.assertTrue(self.b.verify()['balanced'])
 def test_real_clock_regression_still_rejects_future_receipt(self):
  self.book();self.mark();self.now-=1
  packet=self.f.quote_packet('BTCUSDT')
  self.assertIsNotNone(packet['error']);self.assertIn('book_receipt_time',packet['report']['time_rejections'])
