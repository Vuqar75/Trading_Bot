import json,os,sqlite3,tempfile,time,threading,unittest
from pathlib import Path
from research.ai_limit_phase_probe import Probe,TimedConnection
from research.ai_limit_book import Book
from tests.test_ai_quote_stream import QuoteStreamTests

class ProbeTests(unittest.TestCase):
 def make(self,root,**kw):return Probe(Path(root)/'phase.json',resource=lambda:{'sample_ns':time.monotonic_ns(),'test':True},**kw)
 def test_dequeued_source_wait_processing_and_http_are_pinned_per_gap(self):
  class Feed:
   http_timings=[{'path':'/fapi/v1/exchangeInfo','http_ms':500}]
  with tempfile.TemporaryDirectory() as root:
   p=self.make(root)
   try:
    report={'rows':256,'first_id':10,'last_id':265,'oldest_age_ms':5400,'batch_processing_ms':30,'stream':{'oldest_dequeued_wait_ms':5200}}
    p.begin();p.finish(Feed(),{'at_ms':1,'source':{'rows':256}},{'BTCUSDT':report})
    batch=p.ring[-1]['transport']['BTCUSDT']['dequeued_batch']
    self.assertEqual(batch['oldest_dequeued_wait_ms'],5200);self.assertEqual(batch['batch_processing_ms'],30)
    self.assertEqual(p.episodes[0]['http_recent'],Feed.http_timings)
   finally:p.close()
 def test_phase_separates_sleep_from_thread_cpu_and_preserves_exception(self):
  with tempfile.TemporaryDirectory() as root:
   p=self.make(root)
   try:
    p.begin()
    with self.assertRaisesRegex(ValueError,'synthetic'):
     with p.phase('slow'):time.sleep(.025);raise ValueError('synthetic')
    x=p.phases['slow'];self.assertGreater(x['wall_ns'],20000000);self.assertLess(x['thread_cpu_ns'],x['wall_ns']/2)
   finally:p.close()
 def test_sql_timing_commit_fetch_iteration_and_rollback_preserve_rows(self):
  class SlowCommit(TimedConnection):
   def _commit(self):time.sleep(.025);return super()._commit()
  with tempfile.TemporaryDirectory() as root:
   p=self.make(root);b=sqlite3.connect(':memory:',factory=SlowCommit);b.probe=p
   try:
    b.execute('CREATE TABLE x(v)');b.executemany('INSERT INTO x VALUES(?)',[(1,),(2,)]);b.commit()
    self.assertEqual([r[0] for r in b.execute('SELECT v FROM x ORDER BY v')],[1,2])
    self.assertEqual(b.execute('SELECT count(*) FROM x').fetchone()[0],2)
    b.execute('INSERT INTO x VALUES(3)');b.rollback();self.assertEqual(len(b.execute('SELECT v FROM x').fetchall()),2)
    self.assertGreater(p.phases['sql_commit']['wall_ns'],20000000);self.assertIn('sql_step',p.phases);self.assertIn('sql_rollback',p.phases)
   finally:b.close();p.close()
 def test_each_gap_keeps_pre_and_post_cycles_and_later_cycles_do_not_erase_it(self):
  with tempfile.TemporaryDirectory() as root:
   p=self.make(root)
   try:
    for i in range(80):p.begin();p.finish(object())
    p.begin();p.finish(object(),{'at_ms':100,'source':{'rows':256}})
    for i in range(10):p.begin();p.finish(object(),{'at_ms':100})
    p.begin();p.finish(object(),{'at_ms':200})
    self.assertEqual([e['at_ms'] for e in p.episodes],[100,200]);self.assertEqual(len(p.episodes[0]['cycles']),73);self.assertEqual(p.episodes[0]['post_remaining'],0)
    for i in range(100):p.begin();p.finish(object(),{'at_ms':200})
    self.assertEqual(p.episodes[0]['at_ms'],100);self.assertLessEqual(len(p.ring),64)
   finally:p.close()
 def test_slow_diagnostic_disk_is_bounded_and_never_waited_by_main(self):
  entered=threading.Event();release=threading.Event()
  def slow(raw):entered.set();release.wait(1)
  with tempfile.TemporaryDirectory() as root:
   p=self.make(root,write=slow)
   try:
    p.begin();p.finish(object());self.assertTrue(entered.wait(1));started=time.monotonic()
    for i in range(20):p.begin();p.finish(object(),{'at_ms':i+1})
    self.assertLess(time.monotonic()-started,.15);self.assertLessEqual(p.work.qsize(),1);self.assertGreater(p.dropped,0);self.assertLessEqual(len(p.episodes),8)
   finally:release.set();p.close()
 def test_disk_limit_private_permissions_and_no_raw_sql_or_secret(self):
  with tempfile.TemporaryDirectory() as root:
   p=self.make(root,limit=3000)
   try:
    for i in range(60):
     p.begin()
     with p.phase('sql_select'):pass
     p.finish(object(),{'at_ms':i+1})
   finally:p.close()
   raw=(Path(root)/'phase.json').read_bytes();self.assertLessEqual(len(raw),3000);value=json.loads(raw);self.assertGreater(value['evicted_episodes'],0);self.assertEqual((Path(root)/'phase.json').stat().st_mode&0o777,0o600)
 def test_existing_book_instrumentation_preserves_financial_state_and_abort(self):
  with tempfile.TemporaryDirectory() as root:
   directory=Path(root)/'paper';b=Book(directory);before=b.snapshot();b.close();b=Book(directory);p=self.make(root);b.db.probe=p
   try:
    self.assertIsInstance(b.db,TimedConnection)
    self.assertEqual(b.snapshot()['balances'],before['balances'])
    with self.assertRaises(RuntimeError):
     with b.grouped_transaction():b.set('temporary',1);raise RuntimeError()
    self.assertIsNone(b.get('temporary'));self.assertTrue(b.verify()['balanced']);self.assertIn('sql_commit',p.phases)
   finally:b.close();p.close()

class InstrumentedProtectionTests(QuoteStreamTests):
 def setUp(self):
  super().setUp();self.b.close();self.b=Book(self.root,clock=lambda:self.now);self.poller.book=self.b
  self.probe=Probe(self.root/'phase.json',resource=lambda:{'fixture':True});self.b.db.probe=self.probe;self.f.probe=self.probe
 def tearDown(self):self.probe.close();super().tearDown()

if __name__=='__main__':unittest.main()
