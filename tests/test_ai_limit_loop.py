import unittest,tempfile,os,json
from pathlib import Path
from unittest.mock import patch
from research.ai_limit_runtime import ResponseReader,Publisher,atomic
class LoopTests(unittest.TestCase):
 def test_reader_caches_only_successful_unchanged_atomic_identity(self):
  with tempfile.TemporaryDirectory() as temp:
   p=Path(temp)/'response';atomic(p,{'id':1},mode=0o600);reader=ResponseReader()
   with patch('research.ai_limit_runtime.ingest',return_value='done') as ingest:
    reader.apply(None,p,os.geteuid());self.assertEqual(reader.apply(None,p,os.geteuid()),'unchanged');self.assertEqual(ingest.call_count,1)
    atomic(p,{'id':2},mode=0o600);reader.apply(None,p,os.geteuid());self.assertEqual(ingest.call_count,2)
   with patch('research.ai_limit_runtime.ingest',side_effect=ValueError('failure')) as ingest:
    atomic(p,{'id':3},mode=0o600)
    for _ in range(2):
     with self.assertRaises(ValueError):reader.apply(None,p,os.geteuid())
    self.assertEqual(ingest.call_count,2)
 def test_publication_is_bounded_and_forced_once_is_immediate(self):
  pub=Publisher()
  with patch('research.ai_limit_runtime.atomic') as write,patch('research.ai_limit_runtime.time.monotonic_ns',side_effect=[1000000000,1010000000,1250000000,1250000001]):
   self.assertTrue(pub.publish('unused',{}));self.assertFalse(pub.publish('unused',{}));self.assertTrue(pub.publish('unused',{}));self.assertTrue(pub.publish('unused',{},force=True));self.assertEqual(write.call_count,3)

 def test_group_rolls_back_metadata_and_verifies_once_before_commit(self):
  from research.ai_limit_book import Book
  with tempfile.TemporaryDirectory() as temp:
   b=Book(Path(temp)/'paper')
   try:
    with self.assertRaises(RuntimeError):
     with b.grouped_transaction():
      with b.transaction():b.set('rollback_probe',1)
      raise RuntimeError('crash')
    self.assertIsNone(b.get('rollback_probe'))
    with patch.object(b,'verify',wraps=b.verify) as verify:
     with b.grouped_transaction():
      with b.transaction():b.set('probe',1)
      with b.transaction():b.set('probe',2)
     self.assertEqual(verify.call_count,1)
    self.assertTrue(b.verify()['balanced'])
   finally:b.close()

 def test_quiet_socket_read_timeout_keeps_connection_and_cursor(self):
  import json
  from research.ai_limit_stream import StreamFeed
  f=StreamFeed(symbols=['BTCUSDT'],clock=lambda:100000)
  calls=[]
  class Socket:
   def recv(self):
    calls.append(1)
    if len(calls)<=3:raise TimeoutError()
    f.stop.set()
    return json.dumps({'e':'aggTrade','s':'BTCUSDT','a':1,'T':100000,'p':'100','q':'1','m':True})
   def close(self):pass
  f.connect=lambda url:Socket();f.run('BTCUSDT')
  state=f.states['BTCUSDT']
  self.assertEqual(state['read_timeouts'],3);self.assertEqual(state['reconnects'],1)
  self.assertTrue(state['connected']);self.assertEqual(state['last'],1)
  self.assertEqual(state['reason'],'bootstrap')
 def test_true_socket_error_remains_latched_and_classified(self):
  from research.ai_limit_stream import StreamFeed
  f=StreamFeed(symbols=['BTCUSDT']);f.states['BTCUSDT']['fault']=False
  class Socket:
   def recv(self):f.stop.set();raise OSError('not stored')
   def close(self):pass
  f.connect=lambda url:Socket();f.run('BTCUSDT')
  self.assertTrue(f.states['BTCUSDT']['fault']);self.assertFalse(f.states['BTCUSDT']['connected'])
  self.assertEqual(f.states['BTCUSDT']['last_fault']['category'],'OSError')
