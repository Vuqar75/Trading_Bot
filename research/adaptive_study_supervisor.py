"""Single-writer research supervisor; no model, keys, Telegram or orders.

This is an uninstalled prototype. A privileged publisher must supply read-only
evidence, and an external backup mechanism must be verified before deployment.
"""
import argparse,fcntl,json,os,pwd,signal,sqlite3,stat,tempfile,time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing,contextmanager
from pathlib import Path
from . import adaptive_public_feed as feed
from .adaptive_experiment_cycle import cycle
from .adaptive_experiment_init import initialize
from .adaptive_study_status import snapshot
from .adaptive_snapshot_proof import PERIOD,read_trusted,verify_baseline

@contextmanager
def writer_lock(root):
 root=Path(root);fd=os.open(root/'writer.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 try:
  details=os.fstat(fd)
  if not stat.S_ISREG(details.st_mode) or details.st_uid!=os.geteuid() or details.st_mode&0o077:raise ValueError('study_lock_untrusted')
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ValueError('study_writer_already_running') from None
  yield
 finally:os.close(fd)

@contextmanager
def cycle_lock(root):
 fd=os.open(Path(root)/'cycle.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 try:
  details=os.fstat(fd)
  if not stat.S_ISREG(details.st_mode) or details.st_uid!=os.geteuid() or details.st_mode&0o077:raise ValueError('study_lock_untrusted')
  fcntl.flock(fd,fcntl.LOCK_EX);yield
 finally:os.close(fd)

def bootstrap(root,*,clock=lambda:int(time.time()*1000)):
 root=Path(root)
 if root.is_symlink() or not root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):raise ValueError('sandbox_root_required')
 root.mkdir(mode=0o700,parents=True,exist_ok=True)
 meta=root.lstat()
 if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=os.geteuid() or meta.st_mode&0o077:raise ValueError('study_root_untrusted')
 path=root/'study-start.json'
 if path.is_symlink():raise ValueError('study_identity_invalid')
 try:
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 except FileExistsError:
  info=json.loads(path.read_text());created=False
 else:
  created=True
  info={'schema':1,'start_ms':clock()}
  with os.fdopen(fd,'w') as stream:json.dump(info,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
  dfd=os.open(root,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 if info.get('schema')!=1 or type(info.get('start_ms')) is not int:raise ValueError('study_identity_invalid')
 market_db=root/'market-evidence.db'
 if created:
  db=sqlite3.connect(market_db,isolation_level=None)
  try:db.execute('PRAGMA synchronous=FULL');db.execute('CREATE TABLE study_market_meta(start_ms INTEGER NOT NULL)');db.execute('INSERT INTO study_market_meta VALUES(?)',(info['start_ms'],))
  finally:db.close()
 elif market_db.is_symlink() or not market_db.is_file():raise ValueError('market_history_missing')
 with closing(sqlite3.connect(market_db.resolve().as_uri()+'?mode=ro',uri=True)) as db:
  row=db.execute('SELECT start_ms FROM study_market_meta').fetchone()
  if not row or row[0]!=info['start_ms']:raise ValueError('market_identity_conflict')
 for source,name in (('AI','ai'),('baseline','baseline')):initialize(root/name,source=source,start_ms=info['start_ms'])
 return info

def collect_markets(root,*,signal_window,clock=lambda:int(time.time()*1000),provider=feed,filter_cache=None,history_cache=None,warm_history=False,sleeper=time.sleep):
 markets={};errors={}
 if filter_cache is None:filter_cache={}
 if history_cache is None:history_cache={}
 def collect_one(market):
  item={'entry_window':signal_window}
  local_errors={};cached=filter_cache.get(market);now=clock()
  need_filter=not cached or type(cached.get('received_ms')) is not int or not 0<=now-cached['received_ms']<=60000
  # Cache the fully closed signal history before the next boundary. The final
  # skip bar is fetched only after a source-timestamped post-boundary quote.
  remaining=900000-now%900000;warm=history_cache.get(market)
  target=(now//900000+1)*900000
  previous=warm.get('skip_bar') if isinstance(warm,dict) else None
  warm_needed=(warm_history or (not signal_window and remaining<=75000)) and (not isinstance(previous,(tuple,list)) or not previous or previous[0]!=target-1800000)
  if warm_needed:
   try:
    warm=provider.closed_15m(market,clock=clock);history_cache[market]=warm
   except Exception:local_errors[market+'_warm_history']='unavailable'
  if need_filter:
   try:cached=provider.filters(market,clock=clock);filter_cache[market]=cached
   except Exception:cached=None;local_errors[market+'_filters']='unavailable'
  if cached:item['filters']=cached
  boundary=now//900000*900000
  for attempt in range(4 if signal_window else 1):
   try:
    candidate=provider.spot_quote(clock=clock) if market=='spot' else provider.futures_quote(clock=clock)
    if not signal_window or (type(candidate.get('event_ms')) is int and candidate['event_ms']>=boundary):item['quote']=candidate;break
   except Exception:pass
   if not signal_window or clock()>=boundary+3500:break
   sleeper(.3)
  if 'quote' not in item:local_errors[market+'_quote']='unavailable_or_preboundary'
  if signal_window:
   try:
    item['candles']=provider.execution_candles(market,warm,item['quote'],clock=clock)
    item['bars']=item['candles']['bars'];item['skip_bar']=item['candles']['skip_bar'];item['risk_bar']=item['skip_bar']
   except Exception:local_errors[market+'_candles']='unavailable'
  return market,item,local_errors
 with ThreadPoolExecutor(max_workers=2,thread_name_prefix='study-public') as pool:
  for market,item,local_errors in pool.map(collect_one,('spot','futures')):
   markets[market]=item;errors.update(local_errors)
 if markets['futures'].get('quote'):
  due=[]
  for name in ('ai','baseline'):
   p=Path(root)/name/'financial.db'
   with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)) as db:
    row=db.execute("SELECT value FROM futures_meta WHERE key='position'").fetchone();pos=json.loads(row[0]) if row else None
    if pos and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_funding_schedule'").fetchone():
     item=db.execute('SELECT next_due_ms FROM adaptive_funding_schedule WHERE position_id=?',(pos['id'],)).fetchone()
     if item and item[0]<=markets['futures']['quote']['received_ms']:due.append(item[0])
  if due:
   try:markets['futures']['funding_history']=provider.funding_history(min(due)-1,markets['futures']['quote']['received_ms'],clock=clock)
   except Exception:errors['funding_history']='unavailable'
  now=clock();audit_file=Path(root)/'funding-audit.json'
  try:prior=json.loads(audit_file.read_text())
  except (OSError,ValueError,TypeError):prior={}
  last=prior.get('attempt_ms',0)
  if not signal_window and (type(last) is not int or not 0<=now-last<3600000):
   earliest=[]
   for name in ('ai','baseline'):
    p=Path(root)/name/'financial.db'
    with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)) as db:
     if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='funding_evidence'").fetchone():
      row=db.execute('SELECT MIN(event_ms) FROM funding_evidence').fetchone()
      if row and row[0] is not None:earliest.append(row[0])
   if earliest:
    origin=min(earliest)-1;start=prior.get('next_start_ms',origin)
    if type(start) is not int or not origin<=start<=now:start=origin
    end=min(now,start+7*86400000)
    attempt={'attempt_ms':now,'status':'pending','next_start_ms':start,'range_start_ms':start,'range_end_ms':end,'origin_ms':origin};_atomic_json(Path(root),'funding-audit.json',attempt)
    try:
     markets['futures']['funding_audit']=provider.funding_history(start,end,clock=clock)
     attempt['status']='received';markets['futures']['audit_attempt']=attempt
    except Exception:attempt['status']='unavailable';errors['funding_audit']='unavailable'
    _atomic_json(Path(root),'funding-audit.json',attempt)
  elif prior.get('status') in ('unavailable','pending','received'):errors['funding_audit']=prior['status']
 return markets,errors

def run_once(root,*,ai_path,baseline_path,publisher_uid,markets,baseline_uid=None,clock=lambda:int(time.time()*1000)):
 root=Path(root);results={};views={}
 # Same market objects are supplied to both experiments; only the decision
 # source differs. Decision freshness and signal timing are rechecked by cycle.
 for name,source,decision in (('ai','AI',ai_path),('baseline','baseline',baseline_path)):
  try:results[name]=cycle(root/name,source=source,decision_path=decision,publisher_uid=(baseline_uid if name=='baseline' and baseline_uid is not None else publisher_uid),markets=markets,clock=clock)
  except Exception:results[name]={'status':'internal_error_requires_review'}
  stamp=clock();journal=root/name/'observations.db'
  with closing(sqlite3.connect(journal,timeout=10)) as db:
   payload=json.dumps(results[name],sort_keys=True,separators=(',',':'),ensure_ascii=False)
   previous=db.execute('SELECT stamp_ms,payload FROM samples ORDER BY id DESC LIMIT 1').fetchone()
   def signature(item):
    protection=item.get('protection') or {};execution=item.get('result') or {}
    return (item.get('status'),item.get('reason'),protection.get('state'),protection.get('reason'),protection.get('trigger'),execution.get('status'))
   if not previous or previous[0]//900000!=stamp//900000 or signature(json.loads(previous[1]))!=signature(results[name]):db.execute('INSERT INTO samples(stamp_ms,status,payload) VALUES(?,?,?)',(stamp,results[name]['status'],payload))
   db.commit()
  try:views[name]=snapshot(root/name,quotes={k:v['quote'] for k,v in markets.items() if 'quote' in v},now_ms=clock())
  except Exception:views[name]={'source':source,'status':'valuation_or_accounting_unavailable'}
 a=views.get('ai',{}).get('latest_decision');b=views.get('baseline',{}).get('latest_decision')
 return {'checked_ms':clock(),'experiments':views,'cycles':results,'same_snapshot':bool(a and b and a['snapshot_id']==b['snapshot_id'])}

def _atomic_json(root,name,value):
 if name not in ('status.json','funding-audit.json','market-watermarks.json'):raise ValueError('status_name_invalid')
 root=Path(root);fd,tmp=tempfile.mkstemp(prefix='.status-',dir=root)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(value,stream,sort_keys=True,ensure_ascii=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
  os.replace(tmp,root/name);dfd=os.open(root,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def write_status(root,value):_atomic_json(root,'status.json',value)

def update_watermarks(root,markets,*,now_ms,start_ms,signal_window):
 path=Path(root)/'market-watermarks.json'
 try:state=json.loads(path.read_text())
 except (OSError,ValueError,TypeError):state={'spot':{},'futures':{}}
 for market in ('spot','futures'):
  item=markets.get(market,{});view=state.setdefault(market,{})
  quote=item.get('quote')
  if quote:view['quote_event_ms']=quote.get('event_ms');view['quote_received_ms']=quote.get('received_ms')
  boundary=now_ms//900000*900000
  # Even if no loop ran during the two-second admission window, explicitly
  # record the missed boundary on the first subsequent pass; never backfill it.
  if boundary>start_ms and (signal_window or now_ms-boundary>=2000):
   candles=item.get('candles') if signal_window else None;previous=view.get('last_checked_boundary_ms')
   first=(start_ms//900000+1)*900000
   if previous is None and boundary>first:view['missed_windows']=view.get('missed_windows',0)+(boundary-first)//900000
   if type(previous) is int and boundary>previous+900000:view['missed_windows']=view.get('missed_windows',0)+(boundary-previous)//900000-1
   if previous!=boundary:
    if not candles:view['missed_windows']=view.get('missed_windows',0)+1
    view['last_checked_boundary_ms']=boundary
   if candles:
    close_ms=candles['skip_bar'][0]+900000
    if close_ms==boundary:view['last_closed_boundary_ms']=close_ms;view['candles_received_ms']=candles['received_ms']
    else:view['data_conflict']='closed_candle_boundary_mismatch'
 _atomic_json(Path(root),'market-watermarks.json',state);return state

def next_delay(before,after):
 """Do not skip a boundary crossed while the prior cycle was finishing."""
 period=900000
 if before//period<after//period and after%period<2000:return .1
 next_boundary=(after//period+1)*period+300
 return max(.1,min(30.,(next_boundary-after)/1000))

def select_baseline(root,published,*,publisher_uid,start_ms,clock,capture_fetch):
 """Prefer the AI input; collect independent baseline only after its window."""
 root=Path(root);now=clock();fallback=root/'baseline-current.json'
 if now//PERIOD*PERIOD<=start_ms:return Path(published),publisher_uid,'waiting_for_first_future_boundary'
 accepted=None;obs=root/'baseline'/'observations.db'
 if obs.is_file() and not obs.is_symlink():
  with closing(sqlite3.connect(obs.resolve().as_uri()+'?mode=ro',uri=True)) as db:
   row=db.execute('SELECT snapshot_id FROM study_decisions WHERE boundary_ms=?',(now//PERIOD*PERIOD,)).fetchone()
   if row:accepted=row[0]
 for path,uid,label in ((Path(published),publisher_uid,'shared_snapshot'),(fallback,os.geteuid(),'independent_baseline')):
  try:
   captured=read_trusted(path,owner_uid=uid);view=verify_baseline(captured,now_ms=now,start_ms=start_ms)
   if view['boundary_ms']==now//PERIOD*PERIOD and (accepted is None or accepted==view['snapshot_id']):return path,uid,label
  except (OSError,ValueError,KeyError,TypeError):pass
 if accepted is not None:return Path(published),publisher_uid,'accepted_baseline_snapshot_unavailable'
 if now%PERIOD<360000:return Path(published),publisher_uid,'waiting_for_shared_snapshot'
 attempt=root/'baseline-attempt.json'
 try:last=json.loads(attempt.read_text()).get('attempt_ms',0)
 except (OSError,ValueError,TypeError):last=0
 if type(last) is int and 0<=now-last<600000:return Path(published),publisher_uid,'baseline_retry_backoff'
 # Record before network so restarts cannot defeat the ten-minute backoff.
 fd,tmp=tempfile.mkstemp(prefix='.baseline-attempt-',dir=root)
 try:
  with os.fdopen(fd,'w') as stream:json.dump({'attempt_ms':now},stream);stream.flush();os.fsync(stream.fileno())
  os.replace(tmp,attempt)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
 try:
  captured=capture_fetch();verify_baseline(captured,now_ms=clock(),start_ms=start_ms)
  from .adaptive_decision_export import atomic_document
  atomic_document(root,'baseline-current.json',captured)
  return fallback,os.geteuid(),'independent_baseline'
 except Exception:return Path(published),publisher_uid,'baseline_source_unavailable'

def backup_health(path,*,owner_uid,now_ms):
 if path is None or owner_uid is None:return {'state':'unavailable'}
 try:
  value=read_trusted(path,owner_uid=owner_uid);stamp=value['captured_ms'];verified=value['verified_ms'];digest=value['sha256']
  if type(stamp) is not int or type(verified) is not int or not 0<=stamp<=verified<=now_ms or not isinstance(digest,str) or len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest) or not value['object'].startswith('paper-backups/daily/adaptive-continuous/'):raise ValueError('invalid_backup_status')
  return {'state':'fresh' if now_ms-stamp<=36*3600000 else 'stale','captured_ms':stamp,'verified_ms':verified,'sha256':digest}
 except (OSError,ValueError,TypeError,KeyError):return {'state':'unavailable'}

def budget_health(path,*,owner_uid,now_ms):
 if path is None:return {'state':'unavailable'}
 try:
  from .adaptive_study_publisher import validate_budget
  value=validate_budget(read_trusted(path,owner_uid=owner_uid));age=now_ms-value['observed_ms']
  if age<0:raise ValueError('budget_from_future')
  return {'state':'fresh' if age<=600000 else 'stale',**value,'unreserved_micro_usd':value['cap_micro_usd']-value['reserved_micro_usd']}
 except (OSError,ValueError,TypeError,KeyError):return {'state':'unavailable'}

def serve(root,*,ai_path,baseline_path,publisher_uid,version,backup_path=None,backup_uid=None,budget_path=None,stop=lambda:False,clock=lambda:int(time.time()*1000),sleeper=time.sleep,provider=feed,capture_fetch=None):
 root=Path(root)
 if root.is_symlink() or not root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):raise ValueError('sandbox_root_required')
 root.mkdir(mode=0o700,parents=True,exist_ok=True);meta=root.lstat()
 if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=os.geteuid() or meta.st_mode&0o077:raise ValueError('study_root_untrusted')
 with writer_lock(root):
  identity=bootstrap(root,clock=clock)
  filter_cache={};history_cache={}
  if capture_fetch is None:
   from .adaptive_snapshot_proof import capture
   from .adaptive_shadow import get,headlines
   capture_fetch=lambda:capture(get,clock=clock,news_fetch=headlines)
  while not stop():
   before=clock();window=(before%900000)<2000
   markets,errors=collect_markets(root,signal_window=window,clock=clock,provider=provider,filter_cache=filter_cache,history_cache=history_cache)
   selected,baseline_uid,baseline_origin=select_baseline(root,baseline_path,publisher_uid=publisher_uid,start_ms=identity['start_ms'],clock=clock,capture_fetch=capture_fetch)
   with cycle_lock(root):
    audit_data=markets['futures'].get('funding_audit');audit_attempt=markets['futures'].get('audit_attempt')
    if audit_data and audit_attempt:
     from .adaptive_funding_tracker import audit
     all_checked=True
     for name in ('ai','baseline'):
      store=None
      try:
       from trading_bot.futures_store import FuturesStore
       store=FuturesStore(root/name/'financial.db',shared=True,fee_bps='10',slippage_bps='5');audit(store,audit_data)
      except ValueError as exc:
       if str(exc)=='funding_history_conflict_requires_review':errors[name+'_funding_audit']='conflict'
       else:all_checked=False;errors[name+'_funding_audit']='unavailable'
      except Exception:all_checked=False;errors[name+'_funding_audit']='unavailable'
      finally:
       if store is not None:store.close()
     audit_attempt['status']='checked' if all_checked else 'unavailable'
     if all_checked:audit_attempt['next_start_ms']=audit_attempt['range_end_ms']+1 if audit_attempt['range_end_ms']<before else audit_attempt['origin_ms']
     _atomic_json(root,'funding-audit.json',audit_attempt)
    from .adaptive_market_history import ingest
    evidence=sqlite3.connect(root/'market-evidence.db',timeout=10,isolation_level=None)
    try:
     epoch_row=(evidence.execute("SELECT id,start_ms FROM market_epochs WHERE market='futures' ORDER BY id DESC LIMIT 1").fetchone() if evidence.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_epochs'").fetchone() else None)
     for market in ('spot','futures'):
      item=markets[market]
      if market=='futures' and epoch_row:item['epoch_v2']=True
      if item.get('candles'):
       try:
        if market=='futures' and epoch_row:
         from .adaptive_market_epoch import ingest_epoch
         proof=ingest_epoch(evidence,market=market,candles=item['candles'],observed_ms=item['candles']['received_ms'])
        else:proof=ingest(evidence,market=market,candles=item['candles'],observed_ms=item['candles']['received_ms'])
        if proof['entry_blocked']:
         item['entry_conflict']=True;item.pop('bars',None);item.pop('skip_bar',None);item.pop('risk_bar',None);errors[market+'_history']='epoch_wait_for_new_signal' if market=='futures' and epoch_row and not proof.get('conflict') else 'conflict';item['entry_conflict_reason']=errors[market+'_history']
       except (ValueError,TypeError,KeyError):
        item['entry_conflict']=True;item.pop('bars',None);item.pop('skip_bar',None);item.pop('risk_bar',None);errors[market+'_history']='invalid'
      elif market=='futures' and epoch_row:
       item['entry_conflict']=True;errors[market+'_history']='epoch_data_unavailable';item['entry_conflict_reason']='epoch_data_unavailable'
      elif evidence.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_conflicts'").fetchone() and evidence.execute('SELECT 1 FROM market_conflicts WHERE market=? LIMIT 1',(market,)).fetchone():item['entry_conflict']=True;errors[market+'_history']='conflict'
     legacy_conflicts=(evidence.execute("SELECT count(*) FROM market_conflicts WHERE market='futures'").fetchone()[0] if evidence.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_conflicts'").fetchone() else 0)
     epoch_conflicts=(evidence.execute('SELECT count(*) FROM epoch_conflicts WHERE epoch_id=?',(epoch_row[0],)).fetchone()[0] if epoch_row else 0)
    finally:evidence.close()
    result=run_once(root,ai_path=ai_path,baseline_path=selected,publisher_uid=publisher_uid,baseline_uid=baseline_uid,markets=markets,clock=clock)
    watermarks=update_watermarks(root,markets,now_ms=before,start_ms=identity['start_ms'],signal_window=window)
    result.update({'schema':1,'process_pid':os.getpid(),'start_ms':identity['start_ms'],'code_version':version,'data_errors':errors,'signal_window':window,'market_watermarks':watermarks,'next_signal_boundary_ms':(clock()//900000+1)*900000,'baseline_origin':baseline_origin,'backup':backup_health(backup_path,owner_uid=backup_uid,now_ms=clock()),'global_ai_budget':budget_health(budget_path,owner_uid=publisher_uid,now_ms=clock())})
    result['usdm_research']={'epoch_id':epoch_row[0] if epoch_row else None,'epoch_start_ms':epoch_row[1] if epoch_row else None,'legacy_conflicts':legacy_conflicts,'epoch_conflicts':epoch_conflicts,'max_notional_usdt':'100','max_risk_usdt':'5','leverage':1,'limits_status':'preview_only_not_installed_in_execution','execution_gate':'futures_trade_execution_price_unverified','economic_status':'rejected','sizing':None}
    try:
     from .usdm_study_sizing import minimum_futures_size
     ctx=markets['futures'];result['usdm_research']['sizing']=minimum_futures_size(ctx['filters']['filters'],ctx['quote']['price'],stop_distance='.02',fee_bps='10',slippage_bps='5')
     result['usdm_research']['sizing']['price_kind']='indicative_mark_not_execution_price'
    except (ValueError,KeyError,TypeError):result['usdm_research']['sizing_reason']='fresh_filter_or_price_unavailable'
    write_status(root,result)
   sleeper(next_delay(before,clock()))

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--ai',required=True);parser.add_argument('--baseline',required=True);parser.add_argument('--publisher-uid',type=int,required=True);parser.add_argument('--version',required=True);parser.add_argument('--backup-status');parser.add_argument('--backup-user');parser.add_argument('--budget-status')
 args=parser.parse_args();stopping=[False]
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopping.__setitem__(0,True))
 uid=pwd.getpwnam(args.backup_user).pw_uid if args.backup_user else None
 serve(args.root,ai_path=args.ai,baseline_path=args.baseline,publisher_uid=args.publisher_uid,version=args.version,backup_path=args.backup_status,backup_uid=uid,budget_path=args.budget_status,stop=lambda:stopping[0])

if __name__=='__main__':main()
