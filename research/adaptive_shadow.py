"""Public-data-only 4h regime shadow. Never imports trading or secret modules."""
import argparse,datetime as dt,fcntl,hashlib,json,math,os,sqlite3,time,urllib.parse,urllib.request,xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
MODES=('TREND_UP','TREND_DOWN','RANGE','NO_TRADE');PERIOD=4*3600000
HOST='https://data-api.binance.vision';PATHS={'/api/v3/time','/api/v3/klines','/api/v3/depth','/api/v3/ticker/bookTicker'}
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def get(path,params=None):
 if path not in PATHS:raise ValueError('public_endpoint_denied')
 url=HOST+path+('?' + urllib.parse.urlencode(params) if params else '')
 try:
  with urllib.request.urlopen(url,timeout=10) as r:
   raw=r.read(1000001)
  if len(raw)>1000000:raise ValueError('public_data_too_large')
  return json.loads(raw)
 except Exception:raise ValueError('public_data_unavailable') from None
def candles(raw,server,minutes,min_count):
 if not isinstance(raw,list):raise ValueError('candles_invalid')
 step=minutes*60000;out=[]
 for row in raw:
  if not isinstance(row,list) or len(row)<7:raise ValueError('candle_invalid')
  start,end=row[0],row[6]
  if type(start) is not int or type(end) is not int or start%step or end!=start+step-1:raise ValueError('candle_boundary_invalid')
  o,h,l,c,v=map(float,row[1:6])
  if not all(math.isfinite(x) for x in (o,h,l,c,v)) or v<0 or not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('candle_values_invalid')
  if end<server:out.append({'start_ms':start,'close':c,'high':h,'low':l,'volume':v})
 if len(out)<min_count or any(b['start_ms']-a['start_ms']!=step for a,b in zip(out,out[1:])) or out[-1]['start_ms']!=(server//step-1)*step:raise ValueError('candles_stale_or_incomplete')
 return out
def baseline(c):
 if len(c)<20:return 'NO_TRADE'
 close=c[-1]['close'];mean=sum(x['close'] for x in c[-20:])/20
 return 'TREND_UP' if close>mean*1.01 else 'TREND_DOWN' if close<mean*.99 else 'RANGE'
def validate_model(value,snapshot_id,data_ms,now_ms):
 keys={'mode','snapshot_id','data_ms','expires_ms','reasons','uncertainties'}
 if not isinstance(value,dict) or set(value)!=keys or value['mode'] not in MODES or value['snapshot_id']!=snapshot_id:raise ValueError('model_response_invalid')
 if type(value['data_ms']) is not int or value['data_ms']!=data_ms or type(value['expires_ms']) is not int or not value['data_ms']<=now_ms<value['expires_ms']<=value['data_ms']+PERIOD:raise ValueError('model_response_stale')
 for name in ('reasons','uncertainties'):
  if not isinstance(value[name],list) or not 1<=len(value[name])<=8 or any(not isinstance(x,str) or not x or len(x)>400 for x in value[name]):raise ValueError('model_response_invalid')
 return value
def gate(model,*,data_healthy,budget_ready,eligible):
 if not data_healthy:return {'allow':False,'reason':'data_unavailable'}
 if not budget_ready:return {'allow':False,'reason':'budget_executor_unavailable'}
 if not model or model.get('mode')=='NO_TRADE':return {'allow':False,'reason':'model_no_trade_or_unavailable'}
 if not eligible:return {'allow':False,'reason':'no_qualified_strategy'}
 return {'allow':False,'reason':'shadow_only'}
def headlines(clock=lambda:int(time.time()*1000)):
 feeds={'Federal Reserve':'https://www.federalreserve.gov/feeds/press_all.xml','ECB':'https://www.ecb.europa.eu/rss/press.html'};items=[];fail=[]
 for source,url in feeds.items():
  try:
   req=urllib.request.Request(url,headers={'User-Agent':'TradingBot-Adaptive-Shadow/1.0'})
   with urllib.request.urlopen(req,timeout=8) as r:raw=r.read(1000001)
   if len(raw)>1000000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('unsafe_feed')
   root=ET.fromstring(raw);seen=clock()
   for item in root.findall('.//item')[:5]:
    try:
     title=' '.join((item.findtext('title') or '').split())[:250];link=(item.findtext('link') or '').strip();published=parsedate_to_datetime(item.findtext('pubDate') or '')
     parsed=urllib.parse.urlsplit(link)
     if not title or len(link)>250 or parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or any(ord(ch)<32 for ch in link) or published.tzinfo is None:continue
     stamp=int(published.timestamp()*1000)
     if stamp>seen:continue
     items.append({'source':source,'title':title,'url':link,'published_ms':stamp,'observed_ms':seen,'trust':'untrusted_text'})
    except (TypeError,ValueError,OverflowError):continue
  except Exception:fail.append(source)
 return items,fail
def collect(fetch=get,clock=lambda:int(time.time()*1000),news_fetch=headlines):
 start=clock();server=fetch('/api/v3/time')['serverTime']
 if type(server) is not int or abs(server-start)>5000:raise ValueError('clock_skew')
 data={}
 for label,minutes,count in (('15m',15,97),('1h',60,49),('4h',240,20)):
  raw=fetch('/api/v3/klines',{'symbol':'BTCUSDT','interval':label,'limit':min(count+2,200)})
  data[label]=candles(raw,server,minutes,count)
 book_request=clock();book=fetch('/api/v3/ticker/bookTicker',{'symbol':'BTCUSDT'});book_received=clock();depth_request=clock();depth=fetch('/api/v3/depth',{'symbol':'BTCUSDT','limit':20});depth_received=clock();received=depth_received
 bid=float(book['bidPrice']);ask=float(book['askPrice'])
 if book.get('symbol')!='BTCUSDT' or not 0<bid<ask or not isinstance(depth.get('bids'),list) or not isinstance(depth.get('asks'),list) or not depth['bids'] or not depth['asks'] or received-start>30000:raise ValueError('book_invalid_or_slow')
 for row in depth['bids']+depth['asks']:
  if len(row)!=2 or any(not math.isfinite(float(x)) or float(x)<=0 for x in row):raise ValueError('depth_invalid')
 x4=data['4h'];x1=data['1h'];x15=data['15m'];close=x4[-1]['close'];mean=sum(x['close'] for x in x4[-20:])/20
 returns=[x1[i]['close']/x1[i-1]['close']-1 for i in range(len(x1)-20,len(x1))];avg=sum(returns)/len(returns);vol=math.sqrt(sum((x-avg)**2 for x in returns)/len(returns))*100
 news,failed=news_fetch(clock=clock);server_end=fetch('/api/v3/time')['serverTime'];received=clock()
 if type(server_end) is not int or abs(server_end-received)>5000 or any(data[k][-1]['start_ms']!=(server_end//(m*60000)-1)*(m*60000) for k,m in (('15m',15),('1h',60),('4h',240))):raise ValueError('snapshot_crossed_boundary')
 news=[x for x in news if x['observed_ms']<=server_end]
 snap={'schema':1,'symbol':'BTCUSDT','as_of_ms':server_end,'received_ms':received,'latest_4h_ms':x4[-1]['start_ms'],'latest_1h_ms':x1[-1]['start_ms'],'latest_15m_ms':x15[-1]['start_ms'],
       'features':{'close_4h':close,'sma20_4h':mean,'change_4h_pct':100*(close/x4[-2]['close']-1),'volatility_20h_pct':vol,'high48_1h':max(x['close'] for x in x1[-49:-1]),'low48_1h':min(x['close'] for x in x1[-49:-1]),'range_high_24h':max(x['high'] for x in x15[-96:]),'range_low_24h':min(x['low'] for x in x15[-96:]),'volume_15m':x15[-1]['volume'],'average_volume_24h':sum(x['volume'] for x in x15[-96:])/96,'spread_bps':10000*(ask-bid)/((ask+bid)/2),'bid_depth20_notional':sum(float(p)*float(q) for p,q in depth['bids']),'ask_depth20_notional':sum(float(p)*float(q) for p,q in depth['asks'])},
       'book':{'bid':bid,'ask':ask,'bids':depth['bids'],'asks':depth['asks'],'update_id':depth.get('lastUpdateId'),'book_request_ms':book_request,'book_received_ms':book_received,'depth_request_ms':depth_request,'depth_received_ms':depth_received,'source_event_time':'unavailable'},'news':news,'coverage':f'Fed/ECB заголовков: {len(news)}, недоступные ленты: {", ".join(failed) or "нет"}; полнота не подтверждена. Котировка и глубина загружены отдельно, время события источника неизвестно; глубина не доказывает исполнимость.'}
 snap['snapshot_id']=hashlib.sha256(canonical(snap).encode()).hexdigest();return snap,baseline(x4)
def run(state,public,*,fetch=get,clock=lambda:int(time.time()*1000),news_fetch=headlines):
 state=Path(state);public=Path(public);state.mkdir(parents=True,exist_ok=True);public.mkdir(parents=True,exist_ok=True)
 with open(state/'shadow.lock','a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ValueError('shadow_already_running') from None
  db=sqlite3.connect(state/'shadow.sqlite',timeout=5,isolation_level=None)
  try:
   db.execute('PRAGMA synchronous=FULL');db.execute('CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,as_of_ms INTEGER NOT NULL,payload TEXT NOT NULL,baseline TEXT NOT NULL)');db.execute('CREATE TABLE IF NOT EXISTS decisions(boundary_ms INTEGER PRIMARY KEY,snapshot_id TEXT NOT NULL,baseline TEXT NOT NULL,ai_status TEXT NOT NULL)')
   now=clock()
   try:
    snapshot,base=collect(fetch,clock,news_fetch);now=clock();boundary=now//PERIOD*PERIOD;timely=0<=now-boundary<=300000 and snapshot['latest_4h_ms']==boundary-PERIOD
    db.execute('BEGIN IMMEDIATE');db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)',(snapshot['snapshot_id'],snapshot['as_of_ms'],canonical(snapshot),base))
    if timely:db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)',(boundary,snapshot['snapshot_id'],base,'unavailable'))
    decision=db.execute('SELECT snapshot_id,baseline FROM decisions WHERE boundary_ms=?',(boundary,)).fetchone() if timely else None
    db.commit();backup=state/'shadow.backup.sqlite';temporary=state/'shadow.backup.tmp';temporary.unlink(missing_ok=True);copy=sqlite3.connect(temporary)
    try:
     db.backup(copy)
     if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('shadow_backup_invalid')
    finally:copy.close()
    os.chmod(temporary,0o600);os.replace(temporary,backup)
    status={'schema':1,'checked_ms':now,'data_ms':snapshot['as_of_ms'],'snapshot_id':snapshot['snapshot_id'],'baseline':base,'baseline_decision_recorded':bool(decision and decision[0]==snapshot['snapshot_id']),'decision_snapshot_id':decision[0] if decision else None,'decision_baseline':decision[1] if decision else None,'ai_mode':None,'ai_status':'not_available','entry_allowed':False,'entry_reason':'no_qualified_strategy_and_budget_executor_unavailable','next_boundary_ms':(now//PERIOD+1)*PERIOD,'features':snapshot['features'],'coverage':snapshot['coverage'],'sources':snapshot['news'][:3],'budget_status':'original_wsl_budget_not_accessible','api_spend':'unknown_no_calls_by_shadow','book_event_time':'unavailable','local_backup':'verified'}
   except Exception as e:
    if db.in_transaction:db.rollback()
    previous=db.execute('SELECT MAX(as_of_ms) FROM snapshots').fetchone()[0]
    status={'schema':1,'checked_ms':now,'data_ms':None,'last_success_ms':previous,'ai_mode':None,'ai_status':'not_available','baseline':'NO_TRADE','entry_allowed':False,'entry_reason':'data_unavailable','error':str(e) if isinstance(e,ValueError) and str(e) in ('public_data_unavailable','clock_skew','candles_stale_or_incomplete','book_invalid_or_slow') else 'shadow_data_invalid','budget_status':'original_wsl_budget_not_accessible','api_spend':'unknown_no_calls_by_shadow'}
   if status.get('data_ms') is not None:
    input_tmp=public/('.input.'+str(os.getpid()));input_tmp.write_text(json.dumps(snapshot,sort_keys=True,ensure_ascii=False)+'\n');os.chmod(input_tmp,0o644);os.replace(input_tmp,public/'input.json')
   tmp=public/('.status.'+str(os.getpid()));tmp.write_text(json.dumps(status,sort_keys=True,ensure_ascii=False)+'\n');os.chmod(tmp,0o644);os.replace(tmp,public/'status.json');return status
  finally:db.close()
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--state',required=True);p.add_argument('--public',required=True);a=p.parse_args();print(canonical(run(a.state,a.public)))
