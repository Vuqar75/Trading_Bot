"""Publish a narrow, atomic read-only view of the isolated study; no network."""
import argparse,fcntl,json,os,sqlite3,stat,tempfile,time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

def publish(root,directory,*,clock=lambda:int(time.time()*1000)):
 root=Path(root);directory=Path(directory)
 if root.is_symlink() or directory.is_symlink() or not root.is_dir() or not directory.is_dir():raise ValueError('unsafe_directory')
 info=directory.stat()
 if info.st_uid!=os.geteuid() or info.st_mode&0o022:raise ValueError('untrusted_export_directory')
 fd=os.open(root/'cycle.lock',os.O_RDONLY|os.O_NOFOLLOW)
 try:
  fcntl.flock(fd,fcntl.LOCK_SH)
  status=json.loads((root/'status.json').read_text());now=clock()
  if status.get('schema')!=1 or type(status.get('checked_ms')) is not int or status['checked_ms']>now+5000:raise ValueError('status_invalid')
  result={'schema':1,'generated_ms':now,'checked_ms':status['checked_ms'],'start_ms':status['start_ms'],'version':status['code_version'],'backup':status.get('backup'), 'data_errors':status.get('data_errors',{}),'watermarks':status.get('market_watermarks',{}),'same_snapshot':status.get('same_snapshot',False),'experiments':{}}
  # Narrow, non-secret study diagnostics; this does not grant access to ledgers.
  limits=status.get('usdm_research')
  if isinstance(limits,dict):
   result['usdm_research']={k:limits.get(k) for k in ('epoch_id','epoch_start_ms','legacy_conflicts','epoch_conflicts','max_notional_usdt','max_risk_usdt','leverage','limits_status','execution_gate','economic_status','sizing','sizing_reason')}
  for name in ('ai','baseline'):
   v=status['experiments'][name];route=v.get('route') or {};cycle=status.get('cycles',{}).get(name,{})
   clean={k:v.get(k) for k in ('source','cash_usdt','realised_pnl_usdt','open_pnl_usdt','net_pnl_usdt','net_after_known_api_usd','fees_usdt','funding_usdt','closed_trades','ledger_balanced','funding_conflict','recorded_blocked_checks','known_estimated_api_usd','unknown_api_usage')}
   clean['decision']={k:(v.get('latest_decision') or {}).get(k) for k in ('mode','data_ms','answer_ms','snapshot_id')}
   clean['route']={k:route.get(k) for k in ('phase','market','direction','strategy','entry_reason','pending_jobs','protection','decision_fresh','expires_ms')}
   clean['cycle']={k:cycle.get(k) for k in ('status','reason','protection')}
   positions=[]
   with closing(sqlite3.connect((root/name/'financial.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
    for position in v.get('positions',[]):
     market=position.get('market');table='runtime_meta' if market=='spot' else 'futures_meta' if market=='futures' else None
     if table is None:raise ValueError('position_market_invalid')
     row=db.execute('SELECT value FROM '+table+' WHERE key=?',('position',)).fetchone()
     if not row:raise ValueError('position_missing')
     data=json.loads(row[0]);entry=data.get('entry_price') if market=='spot' else data.get('entry')
     qty=str(position.get('quantity'));Decimal(qty);price=Decimal(str(entry));strategy=route.get('strategy')
     factor=Decimal('1.02') if market=='futures' and strategy=='trend1' else Decimal('.98') if market=='spot' and strategy=='trend1' else Decimal('.99') if market=='spot' and strategy=='range' else None
     positions.append({'market':market,'direction':position.get('direction'),'quantity':qty,'entry':str(entry),'strategy':strategy,'protective_level':str(price*factor) if factor is not None else None})
   clean['positions']=positions;result['experiments'][name]=clean
  raw=(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
  if len(raw)>100000:raise ValueError('snapshot_too_large')
  out,tmp=tempfile.mkstemp(prefix='.adaptive-',dir=directory)
  try:
   os.fchmod(out,0o640)
   with os.fdopen(out,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
   os.replace(tmp,directory/'snapshot.json');dfd=os.open(directory,os.O_RDONLY)
   try:os.fsync(dfd)
   finally:os.close(dfd)
  finally:
   if os.path.exists(tmp):os.unlink(tmp)
  return {'generated_ms':now,'checked_ms':status['checked_ms']}
 finally:os.close(fd)

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--directory',required=True);a=p.parse_args()
 print(json.dumps(publish(a.root,a.directory),sort_keys=True))
if __name__=='__main__':main()
