"""Prepare a verifiable export from a durable server decision, without HTTP.

The caller must stage captured data before reserving/sending the request. This
module does not access credentials, write budget records or activate a service.
Its output still requires an independently trusted, read-only publisher.
"""
import hashlib,json,os,stat,tempfile
from decimal import Decimal
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
 durable=db.execute('SELECT response_json,received_ms FROM adaptive_responses WHERE request_id=?',(request_id,)).fetchone()
 if not durable or durable[1]!=record.get('received_ms') or record.get('model')!=model:raise ValueError('durable_response_mismatch')
 raw=json.loads(durable[0])
 if not isinstance(raw,dict) or raw.get('status')!='completed':raise ValueError('durable_response_mismatch')
 texts=[p.get('text') for m in raw.get('output',[]) if isinstance(m,dict) and m.get('type')=='message' and m.get('role')=='assistant' and m.get('status')=='completed' for p in m.get('content',[]) if isinstance(p,dict) and p.get('type')=='output_text']
 if len(texts)!=1 or not isinstance(texts[0],str) or len(texts[0])>20000 or json.loads(texts[0])!=record.get('response'):raise ValueError('durable_response_mismatch')
 original_usage=raw.get('usage');saved_usage=record.get('usage')
 if isinstance(original_usage,dict):
  expected_usage={k:v for k,v in original_usage.items() if k in ('input_tokens','output_tokens','total_tokens') and type(v) is int and v>=0}
  if expected_usage!=saved_usage:raise ValueError('durable_usage_mismatch')
 elif saved_usage is not None:raise ValueError('durable_usage_mismatch')
 from .adaptive_regular import regime_request
 from .adaptive_shadow import canonical
 if hashlib.sha256(canonical(regime_request(snapshot,boundary)).encode()).hexdigest()!=request_sha:raise ValueError('request_hash_mismatch')
 usage=record.get('usage');usage={k:usage.get(k) for k in ('input_tokens','output_tokens')} if isinstance(usage,dict) else None
 document=bind(captured,record.get('response'),record.get('received_ms'),model=model,request_sha256=request_sha,usage=usage)
 checked=verify(document,now_ms=record['received_ms'],start_ms=boundary-1)
 if checked['boundary_ms']!=boundary:raise ValueError('decision_boundary_mismatch')
 return document

def atomic_document(directory,name,value):
 """Write public-only data; no credentials, provider error bodies or budget."""
 directory=Path(directory);info=directory.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or name not in ('baseline-current.json','ai-current.json','budget-current.json'):raise ValueError('invalid_evidence_directory')
 raw=(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
 if len(raw)>2000000:raise ValueError('capture_too_large')
 target=directory/name
 if target.exists():
  if target.is_symlink() or target.stat().st_uid!=os.geteuid() or target.stat().st_mode&0o077:raise ValueError('evidence_file_untrusted')
  if target.read_bytes()==raw:return False
 fd,tmp=tempfile.mkstemp(prefix='.publish-',dir=directory)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,target)
  dfd=os.open(directory,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
 return True

def export_latest(db,directory):
 row=db.execute("SELECT request_id FROM regular_windows WHERE state='completed' AND request_id IS NOT NULL ORDER BY boundary_ms DESC LIMIT 1").fetchone()
 if not row:raise ValueError('durable_decision_missing')
 document=build(db,directory,row[0]);atomic_document(directory,'ai-current.json',document)
 return document['snapshot']['snapshot_id']

def export_budget(db,directory,*,now_ms):
 attempts=db.execute('SELECT count(*),COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts').fetchone()
 missing=db.execute('SELECT count(*) FROM api_attempts a LEFT JOIN api_usage u ON u.request_id=a.request_id WHERE u.request_id IS NULL').fetchone()[0]
 estimated=Decimal(0)
 for row in db.execute('SELECT usage_json FROM api_usage'):
  value=json.loads(row[0]);i=value.get('input_tokens');o=value.get('output_tokens')
  if type(i) is not int or type(o) is not int or i<0 or o<0:raise ValueError('usage_invalid')
  estimated+=(Decimal(i)*Decimal('.1')+Decimal(o)*Decimal('.5'))/Decimal(1000000)
 result={'schema':1,'observed_ms':now_ms,'cap_micro_usd':250000,'reserved_micro_usd':attempts[1],'attempts':attempts[0],'unknown_attempts':missing,'known_estimated_usd':str(estimated),'provider_bill_confirmed':False}
 try:
  old=read_trusted(Path(directory)/'budget-current.json',owner_uid=os.geteuid());age=now_ms-old['observed_ms']
  if 0<=age<300000 and all(old.get(k)==v for k,v in result.items() if k!='observed_ms'):return old
 except (OSError,ValueError,TypeError,KeyError):pass
 atomic_document(directory,'budget-current.json',result);return result
