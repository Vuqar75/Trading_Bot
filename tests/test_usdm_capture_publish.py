import hashlib,json,tempfile,unittest
from pathlib import Path
from research.usdm_capture_publish import publish
from research.adaptive_study_supervisor import capture_coverage
from tests.test_usdm_segment_analysis import s
class PublishTests(unittest.TestCase):
 def test_hash_and_atomic_summary(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);raw=root/'capture';a=s(10000);b=s(15000,4)
   for v in (a,b):v['value']['snapshots']['book']['request_ms']=v['value']['snapshots']['book']['received_ms']-1
   raw.write_text(json.dumps(a)+'\n'+json.dumps(b)+'\n');raw.chmod(0o600)
   digest=hashlib.sha256(raw.read_bytes()).hexdigest()
   with self.assertRaisesRegex(ValueError,'hash_mismatch'):publish(raw,root,expected_sha256='0'*64)
   self.assertEqual(publish(raw,root,expected_sha256=digest)['trade_gaps'],1)
   self.assertEqual(capture_coverage(root)['sequence'],'incomplete')
if __name__=='__main__':unittest.main()
