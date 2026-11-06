"""Protected, verified budget backup and transactional schema preflight."""
import hashlib,json,os,shutil,sqlite3,tempfile,time
from contextlib import closing
from pathlib import Path

APP_ID=0x41494F42
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def attempts(db):return db.execute('select request_id,reserved_micro_usd,created_at,result_json from api_attempts order by request_id').fetchall()
def verify(db):
 if db.execute('pragma application_id').fetchone()[0]!=APP_ID or db.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('budget_integrity_or_id')
 columns=[x[1] for x in db.execute('pragma table_info(api_attempts)')]
 if columns!=['request_id','reserved_micro_usd','created_at','result_json']:raise ValueError('budget_schema_incompatible')
 rows=attempts(db)
 if any(type(r[1]) is not int or r[1]<0 for r in rows):raise ValueError('budget_reservation_invalid')
 return rows
def backup(state,*,root=None):
 state=Path(state);source=state/'experiment-budget.db';root=Path(root) if root else state/'protected-backups'
 if not source.is_file() or not (state/'interactive-once.lock').is_file():raise ValueError('original_budget_or_locks_missing')
 root.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(root,0o700)
 folder=Path(tempfile.mkdtemp(prefix='budget-',dir=root));os.chmod(folder,0o700);target=folder/'experiment-budget.db'
 src=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=15);dst=sqlite3.connect(target)
 try:
  before=verify(src);src.backup(dst);after=verify(dst)
  if before!=after:raise ValueError('backup_concurrent_change')
 finally:dst.close();src.close()
 os.chmod(target,0o600);hashes={'experiment-budget.db':digest(target)}
 for name in ('interactive-once.lock','approved-followup-once.lock','adaptive-next-once.lock'):
  if not (state/name).exists():continue
  p=folder/name;shutil.copyfile(state/name,p);os.chmod(p,0o600);hashes[name]=digest(p)
 with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)) as db:rows=verify(db)
 manifest={'created_utc_ms':int(time.time()*1000),'sha256':hashes,'attempts':len(rows),'reserved_micro_usd':sum(x[1] for x in rows),'usage':'ARCHIVE_ONLY_NOT_ACTIVE'}
 p=folder/'manifest.json';p.write_text(json.dumps(manifest,sort_keys=True)+'\n');os.chmod(p,0o600)
 for f in folder.iterdir():
  with f.open('rb') as handle:os.fsync(handle.fileno())
 fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
 check_backup(folder);return folder,manifest
def check_backup(folder):
 folder=Path(folder);m=json.loads((folder/'manifest.json').read_text())
 if m.get('usage')!='ARCHIVE_ONLY_NOT_ACTIVE' or any(digest(folder/name)!=sha for name,sha in m['sha256'].items()):raise ValueError('backup_hash')
 with tempfile.TemporaryDirectory(prefix='budget-restore-check-') as tmp:
  p=Path(tmp)/'restored.sqlite';shutil.copyfile(folder/'experiment-budget.db',p)
  with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)) as db:rows=verify(db)
  if len(rows)!=m['attempts'] or sum(x[1] for x in rows)!=m['reserved_micro_usd']:raise ValueError('backup_restore_mismatch')
 return m
def migrate(state,*,backup_root=None,fault=lambda stage:None):
 state=Path(state);folder,manifest=backup(state,root=backup_root);fault('backup_verified')
 with closing(sqlite3.connect((folder/'experiment-budget.db').resolve().as_uri()+'?mode=ro',uri=True)) as prior:expected=verify(prior)
 db=sqlite3.connect(state/'experiment-budget.db',timeout=15,isolation_level=None)
 try:
  db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
  if verify(db)!=expected:raise ValueError('budget_changed_since_backup')
  db.execute('CREATE TABLE IF NOT EXISTS api_usage(request_id TEXT PRIMARY KEY,usage_json TEXT NOT NULL)')
  db.execute('CREATE TABLE IF NOT EXISTS api_diagnostics(request_id TEXT PRIMARY KEY,details_json TEXT NOT NULL)')
  db.execute('CREATE TABLE IF NOT EXISTS adaptive_decisions(request_id TEXT PRIMARY KEY,snapshot_json TEXT NOT NULL,baseline TEXT NOT NULL,decision_json TEXT)')
  db.execute('CREATE TABLE IF NOT EXISTS adaptive_responses(request_id TEXT PRIMARY KEY,response_json TEXT NOT NULL,received_ms INTEGER NOT NULL)')
  db.execute('CREATE TABLE IF NOT EXISTS adaptive_send_claims(request_id TEXT PRIMARY KEY)')
  for name,want in {'api_usage':['request_id','usage_json'],'api_diagnostics':['request_id','details_json'],'adaptive_decisions':['request_id','snapshot_json','baseline','decision_json'],'adaptive_responses':['request_id','response_json','received_ms'],'adaptive_send_claims':['request_id']}.items():
   info=db.execute('pragma table_info('+name+')').fetchall()
   if [x[1] for x in info]!=want or not info[0][5]:raise ValueError('budget_schema_incompatible')
  db.execute('INSERT INTO adaptive_responses VALUES (?,?,?)',('__write_probe__','{}',0));db.execute("DELETE FROM adaptive_responses WHERE request_id='__write_probe__'")
  for name,values in (('api_usage',('__write_probe__','{}')),('api_diagnostics',('__write_probe__','{}')),('adaptive_decisions',('__write_probe__','{}','NO_TRADE',None)),('adaptive_send_claims',('__write_probe__',))):
   db.execute('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in values)+')',values);db.execute('DELETE FROM '+name+' WHERE request_id=?',('__write_probe__',))
  if attempts(db)!=expected:raise ValueError('attempts_changed')
  fault('before_migration_commit');db.commit()
 except BaseException:
  if db.in_transaction:db.rollback()
  raise
 finally:db.close()
 return folder,manifest
