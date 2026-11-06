"""Consistent two-account study archive; restore only into isolated copies."""
import argparse,grp,hashlib,io,json,os,re,shutil,sqlite3,tarfile,tempfile,time,urllib.error,urllib.parse,urllib.request,uuid
from contextlib import closing
from pathlib import Path
from .adaptive_sandbox_backup import backup as account_backup,verify as account_verify
from .adaptive_study_supervisor import cycle_lock

BUCKET='tradingbot-paper-backup-101494113814-ew1'
PREFIX='paper-backups/daily/adaptive-continuous/'
FILES=('financial.db','queue.db','observation.db','route.db','manifest.json')
ROOT_OPTIONAL=('market-watermarks.json','baseline-current.json','baseline-attempt.json','funding-audit.json')

def create(root,*,version,clock=lambda:int(time.time()*1000)):
 root=Path(root);start=json.loads((root/'study-start.json').read_text());captured=clock();bundles=[]
 with cycle_lock(root):
  try:
   for name in ('ai','baseline'):
    p=root/name;dest=p/('.archive-'+uuid.uuid4().hex)
    account_backup(p/'financial.db',p/'queue.db',p/'observations.db',p/'route.db',dest)
    account_verify(dest);bundles.append((name,dest))
   manifest={'schema':1,'start_ms':start['start_ms'],'captured_ms':captured,'version':version,'files':{}}
   with tempfile.TemporaryDirectory(prefix='.market-snapshot-',dir=root) as temp:
    files={}
    for name,p in bundles:
     for item in FILES:files[name+'/'+item]=p/item
     files[name+'/study-manifest.json']=root/name/'manifest.json'
    copy=Path(temp)/'market-evidence.db'
    with closing(sqlite3.connect((root/'market-evidence.db').resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(copy)) as dst:
     src.backup(dst)
     if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('market_backup_invalid')
    files['market-evidence.db']=copy
    for name in ROOT_OPTIONAL:
     path=root/name
     if path.exists():
      if path.is_symlink() or not path.is_file():raise ValueError('backup_root_file_invalid')
      files[name]=path
    for name,path in files.items():
     if path.is_symlink() or path.stat().st_size>100000000:raise ValueError('backup_file_too_large')
     with open(path,'rb') as stream:manifest['files'][name]=hashlib.file_digest(stream,'sha256').hexdigest()
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as tar:
     raw=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode();info=tarfile.TarInfo('study-manifest.json');info.size=len(raw);info.mode=0o600;tar.addfile(info,io.BytesIO(raw))
     for name,path in files.items():
      info=tarfile.TarInfo(name);info.size=path.stat().st_size;info.mode=0o600
      with open(path,'rb') as stream:tar.addfile(info,stream)
    blob=out.getvalue()
   if len(blob)>30000000:raise ValueError('backup_too_large')
   verify(blob);return manifest,blob
  finally:
   for _,path in bundles:shutil.rmtree(path)

def verify(blob):
 if len(blob)>30000000:raise ValueError('backup_too_large')
 required={'study-manifest.json','market-evidence.db'}|{name+'/'+item for name in ('ai','baseline') for item in (*FILES,'study-manifest.json')}
 with tempfile.TemporaryDirectory(prefix='study-restore-') as temp:
  root=Path(temp)
  with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as tar:
   members=tar.getmembers();names={x.name for x in members}
   if len(members)!=len(names) or not required<=names or names-required-set(ROOT_OPTIONAL) or any(not x.isfile() or x.size>100000000 for x in members) or sum(x.size for x in members)>300000000:raise ValueError('backup_members_invalid')
   if tar.getmember('study-manifest.json').size>1000000:raise ValueError('backup_manifest_invalid')
   manifest=json.loads(tar.extractfile('study-manifest.json').read())
   if manifest.get('schema')!=1 or set(manifest.get('files',{}))!=names-{'study-manifest.json'}:raise ValueError('backup_manifest_invalid')
   for member in members:
    if member.name=='study-manifest.json':continue
    path=root/member.name;path.parent.mkdir(parents=True,exist_ok=True)
    with tar.extractfile(member) as src,open(path,'wb') as dst:shutil.copyfileobj(src,dst,1024*1024)
    with open(path,'rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if digest!=manifest['files'][member.name]:raise ValueError('backup_hash_mismatch')
  market=root/'market-evidence.db'
  with closing(sqlite3.connect(market.resolve().as_uri()+'?mode=ro',uri=True)) as db:
   if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('SELECT start_ms FROM study_market_meta').fetchone()[0]!=manifest['start_ms']:raise ValueError('backup_market_invalid')
  for name,source in (('ai','AI'),('baseline','baseline')):
   p=root/name
   meta=json.loads((p/'study-manifest.json').read_text())
   if meta.get('source')!=source or meta.get('start_ms')!=manifest['start_ms']:raise ValueError('backup_identity_invalid')
   account_verify(p)
   with closing(sqlite3.connect((p/'observation.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='study_decisions'").fetchone():raise ValueError('backup_observations_missing')
 return manifest

def transfer(blob,manifest,*,transport=None):
 if verify(blob)!=manifest:raise ValueError('backup_mismatch')
 digest=hashlib.sha256(blob).hexdigest();name=PREFIX+str(manifest['captured_ms'])+'-'+digest+'.tar.gz'
 if transport is None:
  request=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',headers={'Metadata-Flavor':'Google'})
  with urllib.request.urlopen(request,timeout=5) as response:token=json.load(response)['access_token']
  headers={'Authorization':'Bearer '+token,'Content-Type':'application/gzip'}
  url='https://storage.googleapis.com/upload/storage/v1/b/'+BUCKET+'/o?'+urllib.parse.urlencode({'uploadType':'media','name':name,'ifGenerationMatch':'0'})
  try:
   with urllib.request.urlopen(urllib.request.Request(url,data=blob,headers=headers,method='POST'),timeout=60) as response:answer=json.load(response)
   if answer.get('name')!=name:raise ValueError('upload_name_mismatch')
  except urllib.error.HTTPError as exc:
   if exc.code!=412:raise ValueError('backup_upload_failed') from None
  url='https://storage.googleapis.com/storage/v1/b/'+BUCKET+'/o/'+urllib.parse.quote(name,safe='')+'?alt=media'
  with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as response:copy=response.read(30000001)
 else:copy=transport(name,blob)
 if hashlib.sha256(copy).hexdigest()!=digest or verify(copy)!=manifest:raise ValueError('backup_readback_mismatch')
 return {'object':name,'sha256':digest,'captured_ms':manifest['captured_ms']}

def stage(root,directory,*,version,clock=lambda:int(time.time()*1000)):
 """Publish a finished private archive to a separate uploader-readable dir."""
 directory=Path(directory)
 if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid!=os.geteuid() or directory.stat().st_mode&0o022:raise ValueError('backup_export_directory_untrusted')
 manifest,blob=create(root,version=version,clock=clock);digest=hashlib.sha256(blob).hexdigest()
 target=directory/('study-'+str(manifest['captured_ms'])+'-'+digest+'.tar.gz')
 fd,tmp=tempfile.mkstemp(prefix='.study-',dir=directory)
 try:
  os.fchmod(fd,0o640)
  with os.fdopen(fd,'wb') as stream:stream.write(blob);stream.flush();os.fsync(stream.fileno())
  try:os.link(tmp,target)
  except FileExistsError:
   if target.is_symlink() or target.read_bytes()!=blob:raise ValueError('backup_identity_conflict')
  dfd=os.open(directory,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:os.unlink(tmp)
 return target

def upload_file(path,*,transport=None):
 path=Path(path)
 if path.is_symlink() or not path.is_file() or path.stat().st_size>30000000:raise ValueError('backup_file_invalid')
 blob=path.read_bytes();manifest=verify(blob)
 match=re.fullmatch(r'study-(\d+)-([0-9a-f]{64})\.tar\.gz',path.name)
 if not match or int(match.group(1))!=manifest['captured_ms'] or match.group(2)!=hashlib.sha256(blob).hexdigest():raise ValueError('backup_identity_mismatch')
 return transfer(blob,manifest,transport=transport)

def publish_status(directory,result,*,group_gid):
 directory=Path(directory)
 if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid!=os.geteuid() or directory.stat().st_mode&0o022:raise ValueError('backup_status_directory_untrusted')
 fd,tmp=tempfile.mkstemp(prefix='.backup-status-',dir=directory)
 try:
  os.fchmod(fd,0o640);os.fchown(fd,os.geteuid(),group_gid)
  with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(result,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
  os.replace(tmp,directory/'last-success.json');dfd=os.open(directory,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def main():
 parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
 a=sub.add_parser('stage');a.add_argument('--root',required=True);a.add_argument('--export-dir',required=True);a.add_argument('--version',required=True)
 b=sub.add_parser('upload');b.add_argument('--export-dir',required=True);b.add_argument('--status-dir',required=True);b.add_argument('--status-group',required=True)
 args=parser.parse_args()
 if args.command=='stage':
  path=stage(args.root,args.export_dir,version=args.version);print(json.dumps({'status':'local_verified','name':path.name}));return
 candidates=[]
 for path in Path(args.export_dir).iterdir():
  match=re.fullmatch(r'study-(\d+)-([0-9a-f]{64})\.tar\.gz',path.name)
  if match and not path.is_symlink() and path.is_file():candidates.append((int(match.group(1)),path))
 if not candidates:raise ValueError('no_completed_backup')
 captured,path=max(candidates,key=lambda x:x[0]);now=int(time.time()*1000)
 if not 0<=now-captured<=36*3600000:raise ValueError('local_backup_stale')
 result=upload_file(path);result['verified_ms']=int(time.time()*1000);publish_status(args.status_dir,result,group_gid=grp.getgrnam(args.status_group).gr_gid)
 print(json.dumps({'status':'external_verified','captured_ms':captured,'sha256':result['sha256']}))

if __name__=='__main__':main()
