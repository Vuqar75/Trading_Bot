"""Publish a bounded completed-segment coverage summary, never a trade."""
import argparse,hashlib,json,os,stat,tempfile
from pathlib import Path
from .usdm_segment_analysis import analyse

def publish(raw_path,state,*,expected_sha256):
 raw_path=Path(raw_path);state=Path(state)
 if raw_path.is_symlink() or state.is_symlink() or not state.is_dir():raise ValueError('unsafe_path')
 info=raw_path.stat()
 if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_size>20*1024*1024:raise ValueError('untrusted_capture')
 blob=raw_path.read_bytes();digest=hashlib.sha256(blob).hexdigest()
 if digest!=expected_sha256:raise ValueError('capture_hash_mismatch')
 rows=[json.loads(line) for line in blob.splitlines()];snap=[v for v in rows if v.get('kind')=='snapshot']
 if not snap:raise ValueError('no_snapshots')
 result=analyse(rows);data={'sha256':digest,'start_ms':snap[0]['value']['snapshots']['book']['request_ms'],'end_ms':snap[-1]['value']['snapshots']['trades']['received_ms'],'snapshots':result['snapshots'],'trade_gaps':result['gaps_detected'],'unusable_time':result['unusable_time']}
 fd,name=tempfile.mkstemp(prefix='.usdm-capture-',dir=state)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w') as f:json.dump(data,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(name,state/'usdm-capture-summary.json');dfd=os.open(state,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(name):os.unlink(name)
 return data

def main():
 p=argparse.ArgumentParser();p.add_argument('--raw',required=True);p.add_argument('--state',required=True);p.add_argument('--sha256',required=True);a=p.parse_args();print(json.dumps(publish(a.raw,a.state,expected_sha256=a.sha256),sort_keys=True))
if __name__=='__main__':main()
