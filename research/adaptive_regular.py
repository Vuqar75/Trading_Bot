"""Prospective, budget-limited, server-only AI shadow; never trades."""
import hashlib,json,os,sqlite3,time
from contextlib import closing
from decimal import Decimal,ROUND_CEILING
from pathlib import Path
from .adaptive_shadow import collect,canonical,PERIOD
from .adaptive_ai_executor import MODEL,ResearchAPIError,request,execute_reserved,recover
from .adaptive_budget_resilience import migrate,verify
from .adaptive_server_once import owner,exclusive,credential
from .adaptive_ai_export import export

CAP=250000;MAX_RESERVE=3640;DAY=86400000;PROTOCOL='regular-shadow-v1';AUTH_PATH=Path('/etc/tradingbot-ai-regular/authorization.json')
def auth(state):
 p=AUTH_PATH
 if p.is_symlink() or not p.is_file():raise ValueError('regular_authorization_missing')
 d=json.loads(p.read_text())
 if set(d)!={'protocol','start_ms','max_daily','cap_micro_usd','max_reserve_micro_usd'} or d['protocol']!=PROTOCOL or d['max_daily']!=6 or d['cap_micro_usd']!=CAP or d['max_reserve_micro_usd']!=MAX_RESERVE or type(d['start_ms']) is not int or d['start_ms']%PERIOD:raise ValueError('regular_authorization_invalid')
 return d
def schema(db):
 db.execute('CREATE TABLE IF NOT EXISTS regular_windows(boundary_ms INTEGER PRIMARY KEY,state TEXT NOT NULL,request_id TEXT,reason TEXT,updated_ms INTEGER NOT NULL)')
 db.execute('CREATE TABLE IF NOT EXISTS regular_horizons(boundary_ms INTEGER NOT NULL,hours INTEGER NOT NULL,close_ms INTEGER NOT NULL,return_pct TEXT NOT NULL,PRIMARY KEY(boundary_ms,hours))')
 db.execute('CREATE TABLE IF NOT EXISTS regular_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
 db.execute('CREATE TABLE IF NOT EXISTS regular_versions(boundary_ms INTEGER PRIMARY KEY,protocol TEXT NOT NULL,model TEXT NOT NULL,request_sha TEXT NOT NULL,snapshot_id TEXT NOT NULL,baseline_version TEXT NOT NULL)')
def initialize(state):
 """One-time migration after a verified archive; not a timer-tick operation."""
 state=Path(state);owner(state);auth(state);migrate(state)
 db=sqlite3.connect(state/'experiment-budget.db',timeout=15,isolation_level=None)
 try:
  db.execute('BEGIN IMMEDIATE');verify(db);schema(db);db.commit()
 except BaseException:
  if db.in_transaction:db.rollback()
  raise
 finally:db.close()
def require_schema(db):
 expected={'regular_windows':['boundary_ms','state','request_id','reason','updated_ms'],'regular_horizons':['boundary_ms','hours','close_ms','return_pct'],'regular_meta':['key','value'],'regular_versions':['boundary_ms','protocol','model','request_sha','snapshot_id','baseline_version']}
 for name,want in expected.items():
  info=db.execute('pragma table_info('+name+')').fetchall()
  if [x[1] for x in info]!=want or not info[0][5]:raise ValueError('regular_schema_incompatible')
def setmeta(db,key,value):db.execute('INSERT OR REPLACE INTO regular_meta VALUES (?,?)',(key,canonical(value)))
def status(db,key):
 r=db.execute('SELECT value FROM regular_meta WHERE key=?',(key,)).fetchone();return json.loads(r[0]) if r else None
def evaluate(db,boundary,snapshot):
 """Only the newly observed closed 4H candle can complete a horizon."""
 close=Decimal(str(snapshot['features']['close_4h']))
 for hours in (4,24):
  origin=boundary-hours*3600000
  row=db.execute("SELECT d.snapshot_json FROM regular_windows w JOIN adaptive_decisions d ON d.request_id=w.request_id WHERE w.boundary_ms=? AND w.state='completed'",(origin,)).fetchone()
  if row:
   old=Decimal(str(json.loads(row[0])['features']['close_4h']))
   if old>0 and close>0:db.execute('INSERT OR IGNORE INTO regular_horizons VALUES (?,?,?,?)',(origin,hours,boundary,str((close/old-1)*100)))
def mark(db,boundary,state,now,reason=None,rid=None):
 db.execute('INSERT OR REPLACE INTO regular_windows VALUES (?,?,?,?,?)',(boundary,state,rid,reason,now))
def counts(db):
 rows=db.execute('SELECT state,count(*) FROM regular_windows GROUP BY state').fetchall();return dict(rows)
def publish(db,state,public,now,blocker=None):
 """Export only validated public status and never call any model."""
 path=Path(public); c=counts(db);total=db.execute('SELECT COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts').fetchone()[0]
 usage=db.execute('SELECT usage_json FROM api_usage').fetchall();i=o=0
 for row in usage:
  u=json.loads(row[0]);i+=u.get('input_tokens',0);o+=u.get('output_tokens',0)
 estimate=(Decimal(i)*Decimal('.1')+Decimal(o)*Decimal('.5'))/Decimal(1000000)
 budget_display=('Успешных регулярных: '+str(c.get('completed',0))+'; неопределённых: '+str(c.get('uncertain',0))+'; пропущенных: '+str(c.get('missed',0))+'; ошибок обработки: '+str(c.get('processing_failed',0))+'. Оценка известных ответов: $'+str(estimate)+'; занято резервами: $'+str(Decimal(total)/1000000)+'; остаток лимита: $'+str(Decimal(CAP-total)/1000000)+'. Два прежних фактических списания неизвестны.')
 if blocker:
  import datetime as dt
  stamp=lambda x:dt.datetime.fromtimestamp(x/1000,dt.timezone.utc).isoformat()
  next_at=dt.datetime.fromtimestamp((now//PERIOD+1)*PERIOD/1000,dt.timezone(dt.timedelta(hours=4))).strftime('%Y-%m-%d %H:%M Баку')
  recent=db.execute('SELECT decision_json,snapshot_json FROM adaptive_decisions WHERE decision_json IS NOT NULL ORDER BY rowid DESC LIMIT 1').fetchone();previous='предыдущего ответа нет';detail={}
  if recent:
   rec=json.loads(recent[0]);decision=rec['response'];label='действует' if now<decision['expires_ms'] else 'устарело'
   previous='последнее решение '+label+': ИИ '+decision['mode']+', baseline '+rec['baseline']
   snap=json.loads(recent[1]);detail={'model':decision['mode'],'baseline':rec['baseline'],'reasons':decision['reasons'],'uncertainties':decision['uncertainties'],'features':snap.get('features',{}),'previous_data_utc':stamp(decision['data_ms']),'previous_answer_utc':stamp(rec['received_ms']),'previous_expiry_utc':stamp(decision['expires_ms'])}
  labels={'waiting_for_first_boundary':'ожидание первой границы','missed':'окно пропущено; задним числом запрос не отправляется','source_unavailable_or_stale':'данные отсутствуют или устарели','uncertain_http':'неопределённая попытка, вызовы приостановлены','budget_exhausted':'общий лимит исчерпан','daily_limit':'дневной лимит достигнут','local_recovery_failed':'ошибка локальной обработки, вызовы приостановлены'}
  d={'schema':1,'data_utc':stamp(now),'answer_utc':stamp(now),'expires_utc':stamp(now+PERIOD),'diagnostic_only':True,'complete':False,'metrics':'Состояние расписания; рыночное решение не обновлено.','coverage':'Предыдущее решение показано с его состоянием свежести.','summary':'ИИ работает в shadow; адаптивные торговые входы запрещены. Состояние расписания: '+labels.get(blocker,'ошибка, требуется проверка')+'; следующая граница '+next_at+'; '+previous,'risks':('Резерв micro-USD: '+str(total)+'; остаток: '+str(CAP-total)+'; регулярных успешных: '+str(c.get('completed',0))+'; неопределённых: '+str(c.get('uncertain',0))+'; ошибок обработки: '+str(c.get('processing_failed',0))+'; пропущенных: '+str(c.get('missed',0))+'; оценка usage USD: '+str(estimate)+'; счёт провайдера не подтверждён.'),'sources':[]}
  detail.update({'budget_display':budget_display,'schedule':'Состояние: '+labels.get(blocker,'ошибка, требуется проверка')+'; следующий пересмотр '+next_at+'.'});d['shadow_details']=detail
  from .adaptive_ai_export import write
  return write(path,d)
 candidate=path.with_name('.regular-candidate.json')
 d=export(Path(state)/'experiment-budget.db',candidate,now)
 from datetime import datetime,timezone,timedelta
 decision=db.execute("SELECT boundary_ms FROM regular_windows WHERE state='completed' ORDER BY boundary_ms DESC LIMIT 1").fetchone()
 if decision:
  next_ms=decision[0]+PERIOD;current=datetime.fromisoformat(d['expires_utc']).timestamp()*1000
  d['expires_utc']=datetime.fromtimestamp(min(next_ms,current)/1000,timezone.utc).isoformat()
  if now>=min(next_ms,current):raise ValueError('regular_decision_expired')
  pairs=db.execute("SELECT json_extract(a.decision_json,'$.response.mode'),a.baseline FROM regular_windows w JOIN adaptive_decisions a ON a.request_id=w.request_id WHERE w.state='completed' ORDER BY w.boundary_ms").fetchall()
  same=sum(x==y for x,y in pairs);switches=sum(pairs[j][0]!=pairs[j-1][0] for j in range(1,len(pairs)));no_trade=sum(x=='NO_TRADE' for x,y in pairs)
  stats='Расписание активно; следующий пересмотр '+datetime.fromtimestamp(next_ms/1000,timezone(timedelta(hours=4))).strftime('%Y-%m-%d %H:%M Баку')+'; совпадений '+str(same)+'/'+str(len(pairs))+'; расхождений '+str(len(pairs)-same)+'; NO_TRADE '+str(no_trade)+'; переключений ИИ '+str(switches)+'. '
  d['summary']=(stats+d['summary']+'; baseline: закрытие против SMA20 4H с порогом 1%; причины ИИ приведены выше.')[:800]
  d['shadow_details']['budget_display']=budget_display
  d['shadow_details']['schedule']='Следующий пересмотр '+datetime.fromtimestamp(next_ms/1000,timezone(timedelta(hours=4))).strftime('%Y-%m-%d %H:%M Баку')+'.'
  horizons={}
  for hours in (4,24):
   values=[Decimal(x[0]) for x in db.execute('SELECT return_pct FROM regular_horizons WHERE hours=?',(hours,))]
   horizons[str(hours)+'h']={'n':len(values),'mean_pct':str(sum(values)/len(values)) if values else 'недостаточно данных'}
  latest=db.execute("SELECT a.decision_json FROM regular_windows w JOIN adaptive_decisions a ON a.request_id=w.request_id WHERE w.state='completed' ORDER BY w.boundary_ms DESC LIMIT 1").fetchone()
  uncertainty='; '.join(json.loads(latest[0])['response']['uncertainties'])[:200] if latest else 'нет данных'
  d['risks']=('ИИ работает в shadow; адаптивные торговые входы запрещены. Время события стакана не подтверждено. Резерв micro-USD '+str(total)+'; остаток '+str(CAP-total)+'; регулярных успешных '+str(c.get('completed',0))+'; неопределённых '+str(c.get('uncertain',0))+'; ошибок обработки '+str(c.get('processing_failed',0))+'; пропусков '+str(c.get('missed',0))+'; известная оценка usage USD '+str(estimate)+'; счёт не подтверждён; движение цены, не PnL: '+str(horizons)+'; неопределённости ИИ: '+uncertainty)[:800]
  from .adaptive_ai_export import write
  return write(path,d)
 return d
def tick(state,public,*,clock=lambda:int(time.time()*1000),fetch=collect,secret=credential,send=execute_reserved):
 state=Path(state);now=clock();boundary=now//PERIOD*PERIOD
 with exclusive(state):
  owner(state);a=auth(state)
  db=sqlite3.connect(state/'experiment-budget.db',timeout=15,isolation_level=None)
  try:
   db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
   if db.execute('PRAGMA application_id').fetchone()[0]!=0x41494F42:raise ValueError('budget_id_mismatch')
   require_schema(db)
   paused=status(db,'paused')
   if paused:
    db.commit();publish(db,state,public,now,paused);return {'status':'paused','reason':paused}
   pending=db.execute("SELECT boundary_ms,request_id FROM regular_windows WHERE state='reserved' ORDER BY boundary_ms LIMIT 1").fetchone()
   if pending:
    saved=db.execute('SELECT 1 FROM adaptive_responses WHERE request_id=?',(pending[1],)).fetchone()
    if not saved:
     mark(db,pending[0],'uncertain',now,'response_not_durable',pending[1]);setmeta(db,'paused','uncertain_http');paused='uncertain_http'
    db.commit()
    if saved:
     try:
      recover(state,pending[1]);db.execute('BEGIN IMMEDIATE');mark(db,pending[0],'completed',now,None,pending[1]);db.commit()
      try:publish(db,state,public,now);setmeta(db,'published_boundary',pending[0])
      except ValueError:pass
      return {'status':'recovered_locally'}
     except Exception:
      if db.in_transaction:db.rollback()
      db.execute('BEGIN IMMEDIATE');mark(db,pending[0],'processing_failed',now,'local_recovery_failed',pending[1]);setmeta(db,'paused','local_recovery_failed');db.commit();paused='local_recovery_failed'
   else:db.commit()
   if paused:publish(db,state,public,now,paused);return {'status':'paused','reason':paused}
   if boundary<a['start_ms']:
    if not status(db,'startup_reported'):
     publish(db,state,public,now,'waiting_for_first_boundary');setmeta(db,'startup_reported',True)
    return {'status':'waiting'}
   last=status(db,'last_boundary');start=max(a['start_ms'],last if type(last) is int else a['start_ms'])
   if (boundary-start)//PERIOD>10000:raise ValueError('unbounded_gap_requires_review')
   if last!=boundary:
    db.execute('BEGIN IMMEDIATE')
    for missed in range(start,boundary,PERIOD):
     if not db.execute('SELECT 1 FROM regular_windows WHERE boundary_ms=?',(missed,)).fetchone():mark(db,missed,'missed',now,'not_observed')
    setmeta(db,'last_boundary',boundary);db.commit()
   row=db.execute('SELECT state FROM regular_windows WHERE boundary_ms=?',(boundary,)).fetchone()
   if row:
    if row[0]=='missed' and status(db,'reported_missed')!=boundary:
     publish(db,state,public,now,'missed');setmeta(db,'reported_missed',boundary)
    if row[0]=='completed' and status(db,'published_boundary')!=boundary:
     try:publish(db,state,public,now);setmeta(db,'published_boundary',boundary)
     except ValueError:pass
    return {'status':row[0]}
   if now-boundary>300000:
    db.execute('BEGIN IMMEDIATE');mark(db,boundary,'missed',now,'window_expired');db.commit();publish(db,state,public,now,'missed');setmeta(db,'reported_missed',boundary);return {'status':'missed'}
   if now-boundary<120000:return {'status':'waiting'}
   try:
    snapshot,baseline=fetch();now=clock()
    if not 120000<=now-boundary<=300000 or snapshot.get('latest_4h_ms')!=boundary-PERIOD or not 0<=now-snapshot['received_ms']<=120000 or abs(snapshot['as_of_ms']-snapshot['received_ms'])>5000:raise ValueError('source_stale_or_wrong_boundary')
    if snapshot.get('snapshot_id')!=hashlib.sha256(canonical({k:v for k,v in snapshot.items() if k!='snapshot_id'}).encode()).hexdigest() or baseline not in ('TREND_UP','TREND_DOWN','RANGE','NO_TRADE'):raise ValueError('snapshot_or_baseline_invalid')
   except Exception:
    publish(db,state,public,clock(),'source_unavailable_or_stale');return {'status':'source_unavailable_or_stale'}
   rid='regular:'+str(boundary);req=request(snapshot)
   req['input'][0]['content']+=' Write every item of reasons and uncertainties in Russian, using plain language. The decision expires no later than '+str(boundary+PERIOD)+' milliseconds UTC; a shorter expiry is retained, never extended. Do not infer unseen events.'
   reserve=int((Decimal(len(canonical(req).encode())+4096)*Decimal('.1')+Decimal(2000)*Decimal('.5')).to_integral_value(rounding=ROUND_CEILING))
   if reserve>MAX_RESERVE:raise ValueError('reserve_limit')
   db.execute('BEGIN IMMEDIATE');require_schema(db);locked_now=clock()
   if not 120000<=locked_now-boundary<=300000 or not 0<=locked_now-snapshot['received_ms']<=120000:db.rollback();raise ValueError('snapshot_stale_after_lock')
   evaluate(db,boundary,snapshot)
   total=db.execute('SELECT COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts').fetchone()[0]
   daily=db.execute('SELECT count(*) FROM api_attempts WHERE created_at>=? AND created_at<?',(boundary//DAY*DAY/1000,(boundary//DAY*DAY+DAY)/1000)).fetchone()[0]
   if daily>=6 or total+reserve>CAP:
    reason='daily_limit' if daily>=6 else 'budget_exhausted'
    if reason=='budget_exhausted':setmeta(db,'paused',reason)
    db.commit();publish(db,state,public,now,reason);return {'status':'paused','reason':reason}
   if db.execute('SELECT 1 FROM regular_windows WHERE boundary_ms=?',(boundary,)).fetchone():db.rollback();return {'status':'already_processed'}
   db.execute('INSERT INTO api_attempts VALUES(?,?,?,NULL)',(rid,reserve,now/1000));db.execute('INSERT INTO adaptive_decisions VALUES(?,?,?,NULL)',(rid,canonical(snapshot),baseline));db.execute('INSERT INTO regular_versions VALUES (?,?,?,?,?,?)',(boundary,PROTOCOL,MODEL,hashlib.sha256(canonical(req).encode()).hexdigest(),snapshot['snapshot_id'],'sma20-4h-1pct-v1'));mark(db,boundary,'reserved',now,None,rid);db.commit()
   old={k:os.environ.get(k) for k in ('OPENAI_API_KEY','TRADING_BOT_AI')};os.environ.pop('OPENAI_API_KEY',None);os.environ['TRADING_BOT_AI']=secret()
   try:
    try:result=send(db,rid,req,clock=clock)
    except Exception:
     if db.execute('SELECT 1 FROM adaptive_responses WHERE request_id=?',(rid,)).fetchone():result=recover(state,rid)
     else:raise
   except Exception as exc:
    if db.in_transaction:db.rollback()
    safe=dict(exc.safe_details) if isinstance(exc,ResearchAPIError) else {'category':'response_or_local_processing_uncertain','automatic_retry':False};safe['observed_ms']=clock()
    saved=db.execute('SELECT 1 FROM adaptive_responses WHERE request_id=?',(rid,)).fetchone();reason='local_recovery_failed' if saved else 'uncertain_http';window_state='processing_failed' if saved else 'uncertain'
    db.execute('BEGIN IMMEDIATE');db.execute('INSERT OR REPLACE INTO api_diagnostics VALUES (?,?)',(rid,canonical(safe)));mark(db,boundary,window_state,clock(),reason,rid);setmeta(db,'paused',reason);db.commit();publish(db,state,public,clock(),reason);return {'status':'paused','reason':reason}
   finally:
    for k,v in old.items():
     if v is None:os.environ.pop(k,None)
     else:os.environ[k]=v
   db.execute('BEGIN IMMEDIATE');mark(db,boundary,'completed',clock(),None,rid);db.commit();publish(db,state,public,clock());setmeta(db,'published_boundary',boundary);return {'status':'completed','mode':result['response']['mode'],'baseline':baseline,'reserve':reserve,'usage':result['usage']}
  finally:db.close()
def main():
 try:result=tick('/var/lib/tradingbot-ai-executor','/var/lib/tradingbot-ai-executor-public/report.json')
 except Exception:result={'status':'data_or_internal_error','network_retry':False}
 print(canonical(result))
if __name__=='__main__':main()
