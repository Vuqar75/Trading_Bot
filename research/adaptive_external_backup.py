"""Consistent shadow SQLite archive; uploads only its own prefixed objects."""
import hashlib,io,json,os,sqlite3,tarfile,tempfile,time,urllib.parse,urllib.request
from pathlib import Path
BUCKET='tradingbot-paper-backup-101494113814-ew1'
PREFIX='paper-backups/daily/adaptive-shadow/'
STATE=Path('/var/lib/adaptive-shadow/shadow.sqlite')
WORK=Path('/var/lib/adaptive-shadow-external')
CODE_VERSION='54e13419c5bf8c13727b4f66c7a5b1caca48ea3b'
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':')).encode()
def snapshot(source,target,clock=time.time):
 src=sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True,timeout=5);dst=sqlite3.connect(target)
 try:
  src.backup(dst)
  if dst.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('snapshot_integrity')
  counts={t:dst.execute('select count(*) from '+t).fetchone()[0] for t in ('snapshots','decisions')}
  latest=dst.execute('select max(as_of_ms) from snapshots').fetchone()[0]
  inconsistent=dst.execute('select count(*) from decisions d left join snapshots s on s.id=d.snapshot_id where s.id is null').fetchone()[0]
  if inconsistent:raise ValueError('decision_snapshot_missing')
 finally:dst.close();src.close()
 raw=Path(target).read_bytes()
 return {'schema':1,'code_version':CODE_VERSION,'captured_ms':int(clock()*1000),'counts':counts,'latest_snapshot_ms':latest,'sqlite_sha256':hashlib.sha256(raw).hexdigest()},raw
def package(manifest,raw):
 buf=io.BytesIO()
 with tarfile.open(fileobj=buf,mode='w:gz') as t:
  for name,data in [('manifest.json',canonical(manifest)),('shadow.sqlite',raw)]:
   info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600;t.addfile(info,io.BytesIO(data))
 return buf.getvalue()
def verify(blob):
 if len(blob)>20000000:raise ValueError('archive_too_large')
 with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as t:
  members=t.getmembers()
  if sorted(x.name for x in members)!=['manifest.json','shadow.sqlite'] or any(not x.isfile() or x.size>15000000 for x in members):raise ValueError('archive_contents')
  manifest=json.loads(t.extractfile('manifest.json').read());raw=t.extractfile('shadow.sqlite').read()
 if hashlib.sha256(raw).hexdigest()!=manifest['sqlite_sha256']:raise ValueError('archive_hash')
 with tempfile.TemporaryDirectory(prefix='adaptive-restore-') as tmp:
  p=Path(tmp)/'shadow.sqlite';p.write_bytes(raw)
  with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
   if db.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('restore_integrity')
   if {t:db.execute('select count(*) from '+t).fetchone()[0] for t in ('snapshots','decisions')}!=manifest['counts']:raise ValueError('restore_counts')
   if db.execute('select max(as_of_ms) from snapshots').fetchone()[0]!=manifest['latest_snapshot_ms']:raise ValueError('restore_latest')
   if db.execute('select count(*) from decisions d left join snapshots s on s.id=d.snapshot_id where s.id is null').fetchone()[0]:raise ValueError('restore_reference')
 return manifest
def transfer(blob,manifest,transport=None):
 digest=hashlib.sha256(blob).hexdigest();name=PREFIX+str(manifest['captured_ms'])+'-'+digest+'.tar.gz'
 if transport is None:
  req=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',headers={'Metadata-Flavor':'Google'})
  with urllib.request.urlopen(req,timeout=5) as r:token=json.load(r)['access_token']
  headers={'Authorization':'Bearer '+token,'Content-Type':'application/gzip'}
  url='https://storage.googleapis.com/upload/storage/v1/b/'+BUCKET+'/o?'+urllib.parse.urlencode({'uploadType':'media','name':name,'ifGenerationMatch':'0'})
  with urllib.request.urlopen(urllib.request.Request(url,data=blob,headers=headers,method='POST'),timeout=60) as r:out=json.load(r)
  if out.get('name')!=name:raise ValueError('upload_name_mismatch')
  url='https://storage.googleapis.com/storage/v1/b/'+BUCKET+'/o/'+urllib.parse.quote(name,safe='')+'?alt=media'
  with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as r:copy=r.read(20000001)
 else:copy=transport(name,blob)
 if hashlib.sha256(copy).hexdigest()!=digest or verify(copy)!=manifest:raise ValueError('readback_mismatch')
 return {'object':name,'sha256':digest,'manifest':manifest}
def run():
 with tempfile.TemporaryDirectory(prefix='adaptive-',dir=WORK) as tmp:
  manifest,raw=snapshot(STATE,Path(tmp)/'shadow.sqlite');blob=package(manifest,raw);verify(blob);result=transfer(blob,manifest)
  tmpfile=WORK/'success.json.tmp';tmpfile.write_bytes(canonical(result));os.chmod(tmpfile,0o600);os.replace(tmpfile,WORK/'success.json')
  print(json.dumps({'status':'verified','object':result['object'],'sha256':result['sha256'],'counts':manifest['counts'],'latest_snapshot_ms':manifest['latest_snapshot_ms']}))
if __name__=='__main__':run()
