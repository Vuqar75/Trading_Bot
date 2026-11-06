import copy,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from research import adaptive_shadow as shadow
from research.adaptive_snapshot_proof import accept,bind,capture,digest,read_trusted,verify,verify_baseline
from tests import test_adaptive_shadow as shadow_fixture

class SnapshotProofTest(unittest.TestCase):
 def setUp(self):
  self.fixture=shadow_fixture.ShadowTest();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.now=self.fixture.now;self.raw={}
  def fetch(path,params=None):
   result=self.fixture.fetch(path,params)
   if path=='/api/v3/klines':self.raw[params['interval']]=result
   return result
  snap,base=shadow.collect(fetch,lambda:self.now,lambda **kw:([],[]))
  decision={'mode':'TREND_UP','snapshot_id':snap['snapshot_id'],'data_ms':self.now,'expires_ms':self.now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.doc={'schema':1,'snapshot':snap,'candles':self.raw,'observed_ms':{k:self.now for k in self.raw},'candle_source':'binance_spot_api_v3_klines','baseline':base,'baseline_version':'sma20-4h-1pct-v1','protocol':'regular-regime-horizon-v2','decision':decision,'answer_ms':self.now+1,'model':'gpt-6-luna','request_sha256':'0'*64}
  self.seal()
 def seal(self):self.doc['sha256']=digest({k:v for k,v in self.doc.items() if k!='sha256'})
 def test_valid_replay_and_boundary_conflict(self):
  db=sqlite3.connect(':memory:',isolation_level=None)
  self.addCleanup(db.close)
  self.assertFalse(accept(db,self.doc,now_ms=self.now+2,start_ms=self.now-1)['replayed'])
  self.assertTrue(accept(db,self.doc,now_ms=self.now+2,start_ms=self.now-1)['replayed'])
  self.doc['decision']['mode']='NO_TRADE';self.seal()
  with self.assertRaisesRegex(ValueError,'boundary_conflict'):accept(db,self.doc,now_ms=self.now+2,start_ms=self.now-1)
 def test_capture_is_same_collection_and_excludes_open_candles(self):
  captured=capture(self.fixture.fetch,clock=lambda:self.now,news_fetch=lambda **kw:([],[]))
  self.assertTrue(all(all(row[6]<self.now for row in rows) for rows in captured['candles'].values()))
  doc=bind(captured,self.doc['decision'],self.now+1,model='gpt-6-luna',request_sha256='0'*64)
  self.assertEqual(verify(doc,now_ms=self.now+2,start_ms=self.now-1)['baseline'],'RANGE')
 def test_baseline_does_not_require_or_invent_model_response(self):
  captured=capture(self.fixture.fetch,clock=lambda:self.now,news_fetch=lambda **kw:([],[]))
  result=verify_baseline(captured,now_ms=self.now+2,start_ms=self.now-1)
  self.assertEqual((result['source'],result['mode']),('baseline','RANGE'))
  self.assertNotIn('response',result)
 def test_tampering_and_missing_candle_are_not_hidden_by_rehash(self):
  self.doc['candles']['4h'][-2][4]='100.5';self.seal()
  with self.assertRaisesRegex(ValueError,'features_mismatch'):verify(self.doc,now_ms=self.now+2,start_ms=self.now-1)
  self.doc['candles']['4h'].pop(-2);self.seal()
  with self.assertRaisesRegex(ValueError,'candles_stale_or_incomplete'):verify(self.doc,now_ms=self.now+2,start_ms=self.now-1)
 def test_baseline_wrong_pair_and_expiry_fail_closed(self):
  for change,expected in ((lambda d:d.update(baseline='TREND_DOWN'),'baseline_mismatch'),(lambda d:d['snapshot'].update(symbol='ETHUSDT'),'symbol_or_schema_invalid'),(lambda d:d['decision'].update(expires_ms=self.now+1),'model_response_stale')):
   before=copy.deepcopy(self.doc);change(self.doc);self.seal()
   with self.assertRaisesRegex(ValueError,expected):verify(self.doc,now_ms=self.now+2,start_ms=self.now-1)
   self.doc=before
 def test_past_answer_and_forged_snapshot_fail(self):
  with self.assertRaisesRegex(ValueError,'not_prospective'):verify(self.doc,now_ms=self.now+2,start_ms=self.now+1)
  self.doc['snapshot']['features']['close_4h']=50;self.seal()
  with self.assertRaisesRegex(ValueError,'snapshot_hash_mismatch'):verify(self.doc,now_ms=self.now+2,start_ms=self.now-1)
 @unittest.skipUnless(os.name=='posix','POSIX file owner and mode required')
 def test_publisher_ownership_mode_and_symlink(self):
  with tempfile.TemporaryDirectory() as temp:
   d=Path(temp);p=d/'export.json';p.write_text(json.dumps(self.doc));p.chmod(0o644)
   self.assertEqual(read_trusted(p,owner_uid=os.getuid())['sha256'],self.doc['sha256'])
   p.chmod(0o666)
   with self.assertRaisesRegex(ValueError,'untrusted_publisher_file'):read_trusted(p,owner_uid=os.getuid())
   p.chmod(0o644);q=d/'link';q.symlink_to(p)
   with self.assertRaises(OSError):read_trusted(q,owner_uid=os.getuid())
