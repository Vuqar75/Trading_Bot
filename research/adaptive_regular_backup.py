"""Encrypted archive of the sole budget; never activates a restored writer."""
import hashlib,io,json,os,sqlite3,subprocess,tarfile,tempfile,time,urllib.parse,urllib.request
from contextlib import closing
from pathlib import Path
from .adaptive_budget_resilience import backup
BUCKET='tradingbot-paper-backup-101494113814-ew1';PREFIX='paper-backups/daily/adaptive-ai-budget/regular/'
def pack(folder,state):
 folder=Path(folder);manifest=json.loads((folder/'manifest.json').read_text());names=['manifest.json']+sorted(manifest['sha256'])
 buf=io.BytesIO()
 with tarfile.open(fileobj=buf,mode='w:gz') as tar:
  for name in names:tar.add(folder/name,arcname=name,recursive=False)
  tar.add(Path(state)/'server-owner.json',arcname='server-owner.json',recursive=False)
 return buf.getvalue()
def verify_plain(data):
 if len(data)>15000000:raise ValueError('archive_too_large')
 with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
  entries=tar.getmembers();names={x.name for x in entries};allowed={'manifest.json','experiment-budget.db','interactive-once.lock','approved-followup-once.lock','adaptive-next-once.lock','server-owner.json'}
  if not {'manifest.json','experiment-budget.db','interactive-once.lock','approved-followup-once.lock','server-owner.json'}<=names or not names<=allowed or len(names)!=len(entries) or any(not x.isfile() or x.size>10000000 for x in entries):raise ValueError('archive_members_invalid')
  m=json.loads(tar.extractfile('manifest.json').read());owner=json.loads(tar.extractfile('server-owner.json').read())
  if owner.get('role')!='ACTIVE_SERVER' or m.get('usage')!='ARCHIVE_ONLY_NOT_ACTIVE' or set(m['sha256'])!=names-{'manifest.json','server-owner.json'}:raise ValueError('archive_manifest_invalid')
  for name,sha in m['sha256'].items():
   if hashlib.sha256(tar.extractfile(name).read()).hexdigest()!=sha:raise ValueError('archive_hash_mismatch')
  raw=tar.extractfile('experiment-budget.db').read()
 with tempfile.TemporaryDirectory(prefix='regular-restore-') as tmp:
  p=Path(tmp)/'restore.db';p.write_bytes(raw)
  with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as db:
   if db.execute('pragma integrity_check').fetchone()[0]!='ok' or db.execute('select count(*),coalesce(sum(reserved_micro_usd),0) from api_attempts').fetchone()!=(m['attempts'],m['reserved_micro_usd']):raise ValueError('restore_mismatch')
 return m
def transfer(blob,manifest,transport=None):
 digest=hashlib.sha256(blob).hexdigest();name=PREFIX+str(manifest['created_utc_ms'])+'-'+digest+'.cred'
 if transport:
  copy=transport(name,blob)
 else:
  req=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',headers={'Metadata-Flavor':'Google'})
  with urllib.request.urlopen(req,timeout=5) as r:token=json.load(r)['access_token']
  headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream'}
  url='https://storage.googleapis.com/upload/storage/v1/b/'+BUCKET+'/o?'+urllib.parse.urlencode({'uploadType':'media','name':name,'ifGenerationMatch':'0'})
  with urllib.request.urlopen(urllib.request.Request(url,data=blob,headers=headers,method='POST'),timeout=60) as r:out=json.load(r)
  if out.get('name')!=name:raise ValueError('upload_name_mismatch')
  url='https://storage.googleapis.com/storage/v1/b/'+BUCKET+'/o/'+urllib.parse.quote(name,safe='')+'?alt=media'
  with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as r:copy=r.read(20000000)
 if hashlib.sha256(copy).hexdigest()!=digest:raise ValueError('readback_mismatch')
 return name,digest,copy
def run(state,work,*,transport=None):
 state=Path(state);work=Path(work);folder,m=backup(state);raw=pack(folder,state);verify_plain(raw)
 with tempfile.TemporaryDirectory(prefix='budget-external-',dir=work) as td:
  encrypted=Path(td)/'budget.cred';p=subprocess.run(['systemd-creds','encrypt','--with-key=host','--name=AI_BUDGET_BACKUP','-',str(encrypted)],input=raw,capture_output=True,timeout=60)
  if p.returncode:raise ValueError('encrypt_failed')
  name,digest,copy=transfer(encrypted.read_bytes(),m,transport);download=Path(td)/'download.cred';download.write_bytes(copy)
  p=subprocess.run(['systemd-creds','decrypt','--name=AI_BUDGET_BACKUP',str(download),'-'],capture_output=True,timeout=60)
  if p.returncode or verify_plain(p.stdout)!=m:raise ValueError('restore_verification_failed')
  result={'object':name,'sha256':digest,'captured_ms':m['created_utc_ms'],'attempts':m['attempts'],'reserved_micro_usd':m['reserved_micro_usd'],'host_bound':True}
  tmp=work/'success.next';tmp.write_text(json.dumps(result,sort_keys=True)+'\n');os.chmod(tmp,0o600);os.replace(tmp,work/'success.json');return result
def main():
 try:result=run('/var/lib/tradingbot-ai-executor','/var/lib/tradingbot-ai-budget-backup');print(json.dumps(result,sort_keys=True))
 except Exception:raise SystemExit('BUDGET_BACKUP_FAILED') from None
if __name__=='__main__':main()
