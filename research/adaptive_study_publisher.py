"""Trusted, atomic read-only study evidence publication; no API or budget IO."""
import argparse,fcntl,grp,json,os,pwd,stat,tempfile
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .adaptive_snapshot_proof import PERIOD,read_trusted,verify,verify_baseline

def validate_budget(value):
 keys={'schema','observed_ms','cap_micro_usd','reserved_micro_usd','attempts','unknown_attempts','known_estimated_usd','provider_bill_confirmed'}
 if not isinstance(value,dict) or set(value)!=keys or value['schema']!=1 or value['cap_micro_usd']!=250000 or value['provider_bill_confirmed'] is not False or any(type(value[k]) is not int or value[k]<0 for k in ('observed_ms','reserved_micro_usd','attempts','unknown_attempts')) or value['reserved_micro_usd']>250000 or value['unknown_attempts']>value['attempts']:raise ValueError('budget_publication_invalid')
 try:cost=Decimal(value['known_estimated_usd'])
 except (InvalidOperation,TypeError,ValueError):raise ValueError('budget_publication_invalid') from None
 if not cost.is_finite() or cost<0:raise ValueError('budget_publication_invalid')
 return value

def publish(source,target,*,source_uid,target_gid,kind):
 """Call as the privileged publisher; study user must never write target dir."""
 target=Path(target);parent=target.parent;meta=parent.lstat()
 if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=os.geteuid() or meta.st_mode&0o022:raise ValueError('untrusted_target_directory')
 fd=os.open(parent/'.publish.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  lockstat=os.fstat(fd)
  if not stat.S_ISREG(lockstat.st_mode) or lockstat.st_uid!=os.geteuid() or lockstat.st_mode&0o077:raise ValueError('untrusted_publisher_lock')
  fcntl.flock(fd,fcntl.LOCK_EX)
  return _publish(source,target,source_uid=source_uid,target_gid=target_gid,kind=kind)
 finally:os.close(fd)

def _publish(source,target,*,source_uid,target_gid,kind):
 parent=target.parent
 if target.is_symlink():raise ValueError('untrusted_target_file')
 value=read_trusted(source,owner_uid=source_uid)
 if kind=='AI':
  answer=value.get('answer_ms');snap=value.get('snapshot',{});data_ms=snap.get('as_of_ms')
  if type(answer) is not int or type(data_ms) is not int:raise ValueError('publication_identity_invalid')
  checked=verify(value,now_ms=answer,start_ms=data_ms//PERIOD*PERIOD-1)
 elif kind=='baseline':
  data_ms=value.get('snapshot',{}).get('as_of_ms')
  if type(data_ms) is not int:raise ValueError('publication_identity_invalid')
  checked=verify_baseline(value,now_ms=data_ms,start_ms=data_ms//PERIOD*PERIOD-1)
 elif kind=='budget':
  validate_budget(value)
  data_ms=value['observed_ms'];checked={'snapshot_id':None}
 else:raise ValueError('publication_kind_invalid')
 if target.exists():
  old=read_trusted(target,owner_uid=os.geteuid())
  old_ms=old.get('observed_ms') if kind=='budget' else old.get('snapshot',{}).get('as_of_ms')
  if type(old_ms) is not int:raise ValueError('old_publication_invalid')
  if old_ms>data_ms:raise ValueError('publication_reordered')
  if old_ms==data_ms:
   if old!=value:raise ValueError('publication_conflict')
   return {'published':False,'snapshot_id':checked['snapshot_id']}
 payload=(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
 fd,tmp=tempfile.mkstemp(prefix='.study-evidence-',dir=parent)
 try:
  with os.fdopen(fd,'wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
  os.chmod(tmp,0o640);os.chown(tmp,os.geteuid(),target_gid);os.replace(tmp,target)
  dfd=os.open(parent,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
 return {'published':True,'snapshot_id':checked['snapshot_id']}

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--source-dir',required=True);parser.add_argument('--target-dir',required=True);parser.add_argument('--source-user',required=True);parser.add_argument('--target-group',required=True)
 args=parser.parse_args();uid=pwd.getpwnam(args.source_user).pw_uid;gid=grp.getgrnam(args.target_group).gr_gid;results={}
 for filename,kind in (('baseline-current.json','baseline'),('ai-current.json','AI'),('budget-current.json','budget')):
  src=Path(args.source_dir)/filename
  if not src.exists():results[kind]='missing';continue
  try:results[kind]='published' if publish(src,Path(args.target_dir)/filename,source_uid=uid,target_gid=gid,kind=kind)['published'] else 'unchanged'
  except Exception:results[kind]='invalid_or_unavailable'
 print(json.dumps(results,sort_keys=True))

if __name__=='__main__':main()
