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
