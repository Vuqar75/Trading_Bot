"""Verification primitives for a future privileged, read-only decision export.

This module does not publish decisions or access the budget, network or secrets.
An authenticated publisher must capture candles when the model snapshot is
constructed; independently fetched candles are not evidence of that snapshot.
"""
import copy,hashlib,json,math,os,sqlite3,stat,time
from pathlib import Path
from .adaptive_shadow import PERIOD,baseline,candles,canonical,validate_model

INTERVALS={'15m':(15,97),'1h':(60,49),'4h':(240,20)}

def digest(value):
 return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()

def capture(fetch,*,clock=lambda:int(time.time()*1000),news_fetch):
 """Capture candles from the actual calls that produce this exact snapshot.

 The caller must persist this evidence before the model request. A separate
 privileged publisher must later bind it to a durable model response.
 """
 from .adaptive_shadow import collect
 raw={};observed={}
 def instrumented(path,params=None):
  result=fetch(path,params);received=clock()
  if path=='/api/v3/klines':
   if not isinstance(params,dict) or params.get('symbol')!='BTCUSDT' or params.get('interval') not in INTERVALS or params['interval'] in raw:raise ValueError('unexpected_candle_request')
   label=params['interval'];raw[label]=copy.deepcopy(result);observed[label]=received
  return result
 snapshot,base=collect(instrumented,clock,news_fetch)
 if set(raw)!=set(INTERVALS):raise ValueError('incomplete_capture')
 # Retain only the actual, fully closed source records. No later refetch.
 raw={k:[r for r in rows if type(r[6]) is int and r[6]<min(observed[k],snapshot['as_of_ms'])] for k,rows in raw.items()}
 return {'snapshot':snapshot,'candles':raw,'observed_ms':observed,'candle_source':'binance_spot_api_v3_klines','baseline':base,'baseline_version':'sma20-4h-1pct-v1','protocol':'regular-regime-horizon-v2'}

def bind(captured,decision,answer_ms,*,model,request_sha256):
 """Prepare an export; transport authentication remains publisher's duty."""
 document={'schema':1,**copy.deepcopy(captured),'decision':copy.deepcopy(decision),'answer_ms':answer_ms,'model':model,'request_sha256':request_sha256}
 document['sha256']=digest(document);return document

def verify(document,*,now_ms,start_ms):
 if not isinstance(document,dict) or set(document)!={'schema','snapshot','candles','observed_ms','candle_source','baseline','baseline_version','protocol','decision','answer_ms','model','request_sha256','sha256'}:raise ValueError('invalid_export')
 if document['schema']!=1 or document['baseline_version']!='sma20-4h-1pct-v1' or document['protocol']!='regular-regime-horizon-v2':raise ValueError('unsupported_version')
 if document['candle_source']!='binance_spot_api_v3_klines' or document['model']!='gpt-6-luna' or not isinstance(document['request_sha256'],str) or len(document['request_sha256'])!=64 or any(c not in '0123456789abcdef' for c in document['request_sha256']):raise ValueError('source_or_model_invalid')
 if digest({k:v for k,v in document.items() if k!='sha256'})!=document['sha256']:raise ValueError('export_hash_mismatch')
 snapshot=document['snapshot'];sources=document['candles'];times=document['observed_ms'];decision=document['decision']
 if not isinstance(snapshot,dict) or not isinstance(sources,dict) or not isinstance(times,dict) or set(sources)!=set(INTERVALS) or set(times)!=set(INTERVALS):raise ValueError('incomplete_input')
 if snapshot.get('symbol')!='BTCUSDT' or snapshot.get('schema')!=1:raise ValueError('symbol_or_schema_invalid')
 sid=snapshot.get('snapshot_id')
 if not isinstance(sid,str) or sid!=digest({k:v for k,v in snapshot.items() if k!='snapshot_id'}):raise ValueError('snapshot_hash_mismatch')
 data_ms=snapshot.get('as_of_ms');answer_ms=document['answer_ms']
 if type(data_ms) is not int or type(answer_ms) is not int or type(start_ms) is not int or type(now_ms) is not int or not start_ms<=data_ms<=answer_ms<=now_ms:raise ValueError('not_prospective')
 if now_ms-data_ms>=PERIOD or data_ms//PERIOD!=answer_ms//PERIOD:raise ValueError('decision_window_invalid')
 validate_model(decision,sid,data_ms,now_ms)
 data={}
 for label,(minutes,count) in INTERVALS.items():
  observed=times[label]
  if type(observed) is not int or not data_ms-30000<=observed<=data_ms+5000:raise ValueError('observation_time_invalid')
  data[label]=candles(sources[label],min(observed,data_ms),minutes,count)
  last=data[label][-1]['start_ms']
  if snapshot.get('latest_'+label+'_ms')!=last or last+minutes*60000>observed:raise ValueError('candle_binding_invalid')
 x4,x1,x15=data['4h'],data['1h'],data['15m'];close=x4[-1]['close'];mean=sum(x['close'] for x in x4[-20:])/20
 returns=[x1[i]['close']/x1[i-1]['close']-1 for i in range(len(x1)-20,len(x1))];avg=sum(returns)/len(returns)
 expected={'close_4h':close,'sma20_4h':mean,'change_4h_pct':100*(close/x4[-2]['close']-1),
           'volatility_20h_pct':math.sqrt(sum((x-avg)**2 for x in returns)/len(returns))*100,
           'high48_1h':max(x['close'] for x in x1[-49:-1]),'low48_1h':min(x['close'] for x in x1[-49:-1]),
           'range_high_24h':max(x['high'] for x in x15[-96:]),'range_low_24h':min(x['low'] for x in x15[-96:]),
           'volume_15m':x15[-1]['volume'],'average_volume_24h':sum(x['volume'] for x in x15[-96:])/96}
 features=snapshot.get('features')
 if not isinstance(features,dict) or any(type(features.get(k)) not in (float,int) or not math.isclose(features[k],v,rel_tol=1e-12,abs_tol=1e-12) for k,v in expected.items()):raise ValueError('features_mismatch')
 if document['baseline']!=baseline(x4):raise ValueError('baseline_mismatch')
 return {'snapshot_id':sid,'data_ms':data_ms,'answer_ms':answer_ms,'mode':decision['mode'],'baseline':document['baseline'],'boundary_ms':data_ms//PERIOD*PERIOD}

def verify_baseline(captured,*,now_ms,start_ms):
 """Validate a trusted capture independently of whether AI replied.

 The synthetic value below is local validation input only; it is never returned
 or published as a model answer. Authentication of the capture is still required.
 """
 snapshot=captured.get('snapshot',{});data_ms=snapshot.get('as_of_ms')
 if type(data_ms) is not int:raise ValueError('incomplete_input')
 baseline_decision={'mode':captured.get('baseline'),'snapshot_id':snapshot.get('snapshot_id'),'data_ms':data_ms,'expires_ms':(data_ms//PERIOD+1)*PERIOD,'reasons':['deterministic baseline'],'uncertainties':['not an AI response']}
 local=bind(captured,baseline_decision,data_ms,model='gpt-6-luna',request_sha256='0'*64)
 checked=verify(local,now_ms=now_ms,start_ms=start_ms)
 return {'source':'baseline','snapshot_id':checked['snapshot_id'],'data_ms':data_ms,'mode':checked['baseline'],'expires_ms':baseline_decision['expires_ms'],'boundary_ms':checked['boundary_ms']}

def read_trusted(path,*,owner_uid):
 """Read a file from a publisher-owned, non-writable-by-others directory.

 Only an independently provisioned privileged publisher may have owner_uid.
 A hash alone is not an authentication mechanism.
 """
 path=Path(path)
 parent=path.parent.lstat()
 if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=owner_uid or parent.st_mode&0o022:raise ValueError('untrusted_publisher_directory')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner_uid or info.st_mode&0o022 or info.st_size>2000000:raise ValueError('untrusted_publisher_file')
  with os.fdopen(fd,'rb',closefd=False) as f:raw=f.read(2000001)
  if len(raw)>2000000:raise ValueError('export_too_large')
  return json.loads(raw)
 finally:os.close(fd)

def accept(db,document,*,now_ms,start_ms):
 """Persist one accepted boundary; an altered replay fails closed."""
 verified=verify(document,now_ms=now_ms,start_ms=start_ms)
 db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS accepted_decisions(boundary_ms INTEGER PRIMARY KEY,snapshot_id TEXT NOT NULL,export_sha TEXT NOT NULL)')
  old=db.execute('SELECT snapshot_id,export_sha FROM accepted_decisions WHERE boundary_ms=?',(verified['boundary_ms'],)).fetchone()
  if old and tuple(old)!=(verified['snapshot_id'],document['sha256']):raise ValueError('boundary_conflict')
  db.execute('INSERT OR IGNORE INTO accepted_decisions VALUES(?,?,?)',(verified['boundary_ms'],verified['snapshot_id'],document['sha256']))
  db.commit();return {**verified,'replayed':bool(old)}
 except BaseException:
  db.rollback();raise
