"""SQLite snapshot, immutable prefixed GCS object, verified readback; no key files."""
import hashlib,io,json,os,sqlite3,tarfile,tempfile,time,urllib.parse,urllib.request
from pathlib import Path

BUCKET='tradingbot-paper-backup-101494113814-ew1';PREFIX='paper-backups/daily/donchian60/'
STATE=Path('/var/lib/donchian-observer/observer.sqlite');WORK=Path('/var/lib/donchian-backup')
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':')).encode()
def snapshot(source,target,clock=time.time):
    src=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True,timeout=5);dst=sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('snapshot_integrity')
        meta={k:json.loads(v) for k,v in dst.execute("SELECT key,value FROM meta WHERE key IN ('start_ms','version','quote_protocol','next_boundary','halted')")}
        if set(meta)<{'start_ms','version','next_boundary','halted'}:raise ValueError('snapshot_metadata')
        latest={s:t for s,t in dst.execute('SELECT symbol,MAX(open_ms) FROM candles GROUP BY symbol')};counts={t:dst.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('events','decisions','candles','candle_versions')}
        transition=dst.execute("SELECT payload FROM events WHERE id='infra:quote-protocol-v2'").fetchone();adapter=json.loads(transition[0])['adapter_sha256'] if transition else None
    finally:dst.close();src.close()
    raw=Path(target).read_bytes();return {'schema':1,'captured_utc_ms':int(clock()*1000),'start_ms':meta['start_ms'],'strategy_sha256':meta['version'],'adapter_sha256':adapter,'quote_protocol':meta.get('quote_protocol','ticker-price-v1'),'next_boundary_ms':meta['next_boundary'],'halted':meta['halted'],'latest_candle_ms':latest,'counts':counts,'sqlite_sha256':hashlib.sha256(raw).hexdigest()},raw
def package(manifest,raw):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as tar:
        for name,data in [('manifest.json',canonical(manifest)),('observer.sqlite',raw)]:
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600;tar.addfile(info,io.BytesIO(data))
    return out.getvalue()
def verify(data):
    if len(data)>20_000_000:raise ValueError('archive_too_large')
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
        members=tar.getmembers()
        if sorted(x.name for x in members)!=['manifest.json','observer.sqlite'] or any(not x.isfile() for x in members):raise ValueError('archive_contents')
        if any(x.size>15_000_000 for x in members):raise ValueError('archive_member_size')
        manifest=json.loads(tar.extractfile('manifest.json').read());raw=tar.extractfile('observer.sqlite').read()
    if hashlib.sha256(raw).hexdigest()!=manifest['sqlite_sha256']:raise ValueError('archive_hash')
    with tempfile.TemporaryDirectory(prefix='donchian-restore-') as tmp:
        target=Path(tmp)/'observer.sqlite';target.write_bytes(raw)
        with sqlite3.connect('file:'+str(target)+'?mode=ro',uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('restore_integrity')
            start=json.loads(db.execute("SELECT value FROM meta WHERE key='start_ms'").fetchone()[0]);counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('events','decisions','candles','candle_versions')}
            if start!=manifest['start_ms'] or counts!=manifest['counts']:raise ValueError('restore_state_mismatch')
    return manifest
def metadata_token():
    req=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',headers={'Metadata-Flavor':'Google'})
    with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)['access_token']
def transfer(data,manifest,transport=None):
    digest=hashlib.sha256(data).hexdigest();name=PREFIX+str(manifest['captured_utc_ms'])+'-'+digest+'.tar.gz'
    if transport is None:
        token=metadata_token();headers={'Authorization':'Bearer '+token,'Content-Type':'application/gzip'}
        url='https://storage.googleapis.com/upload/storage/v1/b/'+BUCKET+'/o?'+urllib.parse.urlencode({'uploadType':'media','name':name,'ifGenerationMatch':'0'})
        req=urllib.request.Request(url,data=data,headers=headers,method='POST')
        with urllib.request.urlopen(req,timeout=60) as r:out=json.load(r)
        if out.get('name')!=name:raise ValueError('upload_name_mismatch')
        url='https://storage.googleapis.com/storage/v1/b/'+BUCKET+'/o/'+urllib.parse.quote(name,safe='')+'?alt=media'
        with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as r:copy=r.read(20_000_001)
    else:copy=transport(name,data)
    if hashlib.sha256(copy).hexdigest()!=digest or verify(copy)!=manifest:raise ValueError('readback_mismatch')
    return {'object':name,'sha256':digest,'manifest':manifest}
def run():
    WORK.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='snapshot-',dir=WORK) as tmp:
        manifest,raw=snapshot(STATE,Path(tmp)/'observer.sqlite');data=package(manifest,raw);verify(data);result=transfer(data,manifest)
        marker=WORK/'success.json.tmp';marker.write_bytes(canonical(result));os.chmod(marker,0o600);os.replace(marker,WORK/'success.json')
        print(json.dumps({'status':'verified','object':result['object'],'sha256':result['sha256'],'bytes':len(data),'start_ms':manifest['start_ms'],'captured_ms':manifest['captured_utc_ms']}),flush=True)
if __name__=='__main__':run()
