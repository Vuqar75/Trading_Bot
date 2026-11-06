"""Coordinated offline route+paper backup; never restores over live state."""
import hashlib,json,os,shutil,sqlite3,tempfile,time
from contextlib import closing
from pathlib import Path
from trading_bot.paper_coordinated_backup import coordinated_backup

def backup(financial,queue,observation,route,destination,*,timeout=10):
 sources=[Path(x).resolve(strict=True) for x in (financial,queue,observation,route)]
 root=Path(tempfile.gettempdir()).resolve();destination=Path(destination).absolute()
 if (len(set(sources))!=4 or any(not p.is_relative_to(root) for p in sources) or
     len({p.parent for p in sources})!=1 or destination.exists() or destination.parent.resolve()!=sources[0].parent):raise ValueError('sandbox_paths_required')
 stage=Path(tempfile.mkdtemp(prefix='.adaptive-backup-',dir=destination.parent));os.chmod(stage,0o700)
 lock=sqlite3.connect(sources[3].as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=timeout)
 try:
  if lock.execute('pragma journal_mode').fetchone()[0].lower()!='delete':raise ValueError('rollback_journal_required')
  lock.execute('BEGIN IMMEDIATE')
  bundle=stage/'bundle';manifest=coordinated_backup(*sources[:3],bundle,timeout=timeout)
  out=bundle/'route.db'
  with closing(sqlite3.connect(sources[3].as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(out)) as dst:
   if not src.execute("select 1 from sqlite_master where type='table' and name='route_state'").fetchone():raise ValueError('route_schema_missing')
   src.backup(dst)
   if dst.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('route_integrity_failure')
  digest=hashlib.sha256(out.read_bytes()).hexdigest();manifest['files'].append({'name':'route.db','sha256':digest})
  (bundle/'manifest.json').write_text(json.dumps(manifest,sort_keys=True));os.chmod(bundle/'manifest.json',0o600)
  for path in (out,bundle/'manifest.json'):
   with open(path,'rb') as f:os.fsync(f.fileno())
  fd=os.open(bundle,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
  if destination.exists():raise ValueError('destination_exists')
  os.rename(bundle,destination)
  fd=os.open(destination.parent,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
  return manifest
 finally:
  if lock.in_transaction:lock.rollback()
  lock.close();shutil.rmtree(stage)

def verify(bundle):
 p=Path(bundle);manifest=json.loads((p/'manifest.json').read_text());items=manifest['files']
 if {x['name'] for x in items}!={'financial.db','queue.db','observation.db','route.db'}:raise ValueError('manifest_invalid')
 for item in items:
  path=p/item['name']
  if path.is_symlink():raise ValueError('hash_mismatch')
  with open(path,'rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
  if digest!=item['sha256']:raise ValueError('hash_mismatch')
  with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
   if db.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('integrity_failure')
 return manifest
