"""Prepare a verifiable export from a durable server decision, without HTTP.

The caller must stage captured data before reserving/sending the request. This
module does not access credentials, write budget records or activate a service.
Its output still requires an independently trusted, read-only publisher.
"""
import hashlib,json,os,stat,tempfile
from pathlib import Path
from .adaptive_snapshot_proof import bind,digest,read_trusted,verify

def stage(directory,captured):
 directory=Path(directory)
 info=directory.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('invalid_evidence_directory')
 snapshot=captured.get('snapshot',{});sid=snapshot.get('snapshot_id')
 if not isinstance(sid,str) or len(sid)!=64 or any(x not in '0123456789abcdef' for x in sid) or sid!=digest({k:v for k,v in snapshot.items() if k!='snapshot_id'}):raise ValueError('snapshot_hash_mismatch')
 payload=json.dumps(captured,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
 if len(payload)>2000000:raise ValueError('capture_too_large')
 dest=directory/(sid+'.json');fd,tmp=tempfile.mkstemp(prefix='.capture-',dir=directory)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
  try:os.link(tmp,dest)
  except FileExistsError:
   if dest.is_symlink() or dest.read_bytes()!=payload:raise ValueError('capture_identity_conflict')
  dfd=os.open(directory,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:os.unlink(tmp)
 return dest

def build(db,directory,request_id):
 row=db.execute("SELECT w.boundary_ms,w.state,d.snapshot_json,d.baseline,d.decision_json,v.protocol,v.model,v.request_sha,v.snapshot_id,v.baseline_version FROM regular_windows w JOIN adaptive_decisions d ON d.request_id=w.request_id JOIN regular_versions v ON v.boundary_ms=w.boundary_ms WHERE w.request_id=?",(request_id,)).fetchone()
 if not row or row[1]!='completed' or not row[4]:raise ValueError('durable_decision_missing')
 boundary,_,snapshot_json,base,record_json,protocol,model,request_sha,sid,baseline_version=row
 p=Path(directory)/(sid+'.json')
 if p.is_symlink() or not p.is_file() or p.stat().st_size>2000000:raise ValueError('capture_missing')
 captured=read_trusted(p,owner_uid=os.geteuid());snapshot=json.loads(snapshot_json);record=json.loads(record_json)
 if captured.get('snapshot')!=snapshot or captured.get('baseline')!=base or record.get('baseline')!=base or captured.get('protocol')!=protocol or captured.get('baseline_version')!=baseline_version or snapshot.get('snapshot_id')!=sid:raise ValueError('decision_capture_mismatch')
 from .adaptive_regular import regime_request
 from .adaptive_shadow import canonical
 if hashlib.sha256(canonical(regime_request(snapshot,boundary)).encode()).hexdigest()!=request_sha:raise ValueError('request_hash_mismatch')
 document=bind(captured,record.get('response'),record.get('received_ms'),model=model,request_sha256=request_sha)
 checked=verify(document,now_ms=record['received_ms'],start_ms=boundary)
 if checked['boundary_ms']!=boundary:raise ValueError('decision_boundary_mismatch')
 return document

def atomic_document(directory,name,value):
 """Write public-only data; no credentials, provider error bodies or budget."""
 directory=Path(directory);info=directory.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or name not in ('baseline-current.json','ai-current.json'):raise ValueError('invalid_evidence_directory')
 raw=(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
 if len(raw)>2000000:raise ValueError('capture_too_large')
 fd,tmp=tempfile.mkstemp(prefix='.publish-',dir=directory)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,directory/name)
  dfd=os.open(directory,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def export_latest(db,directory):
 row=db.execute("SELECT request_id FROM regular_windows WHERE state='completed' AND request_id IS NOT NULL ORDER BY boundary_ms DESC LIMIT 1").fetchone()
 if not row:raise ValueError('durable_decision_missing')
 document=build(db,directory,row[0]);atomic_document(directory,'ai-current.json',document)
 return document['snapshot']['snapshot_id']
