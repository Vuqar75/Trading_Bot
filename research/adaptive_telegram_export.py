"""Publish a narrow, atomic read-only view of the isolated study; no network."""
import argparse,datetime,fcntl,json,os,sqlite3,stat,tempfile,time,math
from contextlib import closing
from decimal import Decimal
from pathlib import Path

def last_hourly_short(root,*,now_ms):
 """Read an actually evaluated hourly SHORT; never submit or amend a job."""
 from .adaptive_route_policy import closed_bar_signal,candle_digest
 root=Path(root)
 try:
  with closing(sqlite3.connect((root/'baseline'/'queue.db').resolve(strict=True).as_uri()+'?mode=ro',uri=True)) as q,closing(sqlite3.connect((root/'baseline'/'observations.db').resolve(strict=True).as_uri()+'?mode=ro',uri=True)) as o:
   jobs=q.execute("SELECT id,status,payload FROM unified_jobs WHERE command='/research_adaptive_entry' AND market='futures' ORDER BY id DESC LIMIT 32").fetchall()
   for jid,state,payload in jobs:
    data=json.loads(payload);bars=data['bars'];skip=data['skip_bar'];boundary=skip[0]+900000
    if state!='FAILED' or type(boundary) is not int or not 0<boundary<=now_ms or not bars or bars[-1][0]+900000!=skip[0]:continue
    same_boundary=sum(1 for _,_,other in jobs if (json.loads(other).get('skip_bar') or [None])[0]==skip[0])
    if same_boundary!=1:continue
    if candle_digest(bars,skip)!=data.get('candles_sha256'):continue
    sig=closed_bar_signal(bars,'trend1',now_ms=bars[-1][0]+900000)
    if not sig or sig[0]!='TREND':continue
    samples=o.execute("SELECT stamp_ms,payload FROM samples WHERE status='entry_evaluated' AND stamp_ms BETWEEN ? AND ? ORDER BY stamp_ms LIMIT 3",(boundary,boundary+15000)).fetchall()
    for stamp,raw in samples:
     sample=json.loads(raw);route=sample.get('route') or {};outcome=(sample.get('result') or {}).get('status')
     if route.get('market')!='futures' or route.get('direction')!='SHORT' or route.get('strategy')!='trend1' or outcome!='NO_SIGNAL' or sig[1]<sig[3]:continue
     if not all(isinstance(x,(int,float)) and math.isfinite(x) and x>0 for x in (sig[1],sig[3])):continue
     return {'signal_close_ms':bars[-1][0]+900000,'evaluated_ms':stamp,'close':sig[1],'threshold':sig[3],'result':'no_short_signal','job_id':jid,'candles_sha256':data['candles_sha256']}
 except (OSError,sqlite3.Error,ValueError,KeyError,TypeError,IndexError,json.JSONDecodeError):pass
 return None

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
   result['usdm_research']={k:limits.get(k) for k in ('epoch_id','epoch_start_ms','legacy_conflicts','epoch_conflicts','active_epoch_conflicts','max_notional_usdt','max_risk_usdt','leverage','limits_status','execution_gate','economic_status','capture','candle_lifecycle','sizing','sizing_reason')}
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
  baseline=result['experiments']['baseline'];signal=last_hourly_short(root,now_ms=status['checked_ms'])
  if signal:
   baseline['last_hourly_signal']=signal
   # Compatibility with the deployed read-only renderer: preserve the
   # canonical values separately and render only while flat and waiting.
   route=baseline['route'];cycle=baseline['cycle']
   if route.get('phase')=='IDLE' and not baseline['positions'] and cycle.get('status')=='waiting_for_signal_window' and not status.get('data_errors'):
    when=datetime.datetime.fromtimestamp(signal['signal_close_ms']/1000,datetime.timezone(datetime.timedelta(hours=4))).strftime('%d.%m %H:%M')
    detail=f"свеча {when} Баку: {signal['close']} ≥ мин48 {signal['threshold']}; SHORT нет"
    route['operational_strategy']=route.get('strategy');route['strategy']='тренд SHORT · '+detail
    cycle['operational_status']=cycle['status'];cycle['status']=detail
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
