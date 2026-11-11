"""Fixed sequential hourly XGB/Ridge study; reads pinned files, no live access."""
import calendar,csv,datetime as dt,gzip,hashlib,io,json,math,pathlib,sys,zipfile
from collections import Counter
import numpy as np
import scipy,sklearn,xgboost
from sklearn.linear_model import Ridge
from xgboost import XGBRegressor
from research import extreme_reversal as execution

M=execution.M;START=execution.START;END=execution.END;N=execution.N
PRED_START=execution.stamp('2025-07-01');THRESHOLD=.00375

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()

def load_stream(root,symbol):
 manifest=json.loads((root/'extreme-restore-manifest.json').read_text(encoding='utf8'))
 if manifest['unavailable']:raise ValueError('pinned input missing')
 trade=np.full((N,5),np.nan);mark=np.full((N,4),np.nan);fund=[];proof=[];conflicts=0
 files=[x for x in manifest['files'] if symbol in x['file']];files.sort(key=lambda x:('/rest/' in x['file'],x['file']))
 for x in files:
  path=root/x['file'];digest=sha(path)
  if digest!=x['sha256']:raise ValueError('input hash mismatch')
  proof.append({'file':x['file'],'sha256':digest})
  name=x['file'];isrest='/rest/' in name;kind='funding' if '/fundingRate/' in '/'+name else 'mark' if 'markPriceKlines' in name else 'trade'
  def consume(rows):
   nonlocal conflicts
   for r in rows:
    if not str(r[0]).isdigit():continue
    t=int(r[0])
    if kind=='funding':
     rate=float(r[2]);interval=int(r[1])
     if interval<=0 or not math.isfinite(rate):raise ValueError('bad funding')
     fund.append((t,rate));continue
    if t%M or int(r[6])!=t+M-1:raise ValueError('bad minute time')
    i=execution.ix(t)
    if not 0<=i<N:continue
    o,h,l,c=map(float,r[1:5]);v=float(r[5])
    if not 0<l<=min(o,c)<=max(o,c)<=h or not math.isfinite(v) or v<0:raise ValueError('bad OHLCV')
    a=mark if kind=='mark' else trade;values=np.array((o,h,l,c) if kind=='mark' else (o,h,l,c,v))
    if np.isfinite(a[i,0]):
     if not isrest:raise ValueError('duplicate archive minute')
     if not np.array_equal(a[i],values):conflicts+=1
    else:a[i]=values
  if isrest:
   with path.open(encoding='utf8') as f:consume(json.load(f))
  else:
   with zipfile.ZipFile(path) as z:
    names=z.namelist()
    if len(names)!=1:raise ValueError('unexpected zip member')
    with z.open(names[0]) as stream:consume(csv.reader(io.TextIOWrapper(stream,encoding='utf8')))
 fund.sort();slots=[t//(8*60*M) for t,_ in fund]
 if len(set(slots))!=len(slots) or any(b-a!=1 for a,b in zip(slots,slots[1:])):raise ValueError('funding gap')
 if not fund or fund[0][0]>execution.BEGIN or fund[-1][0]<END-8*60*M:raise ValueError('funding coverage')
 return trade,mark,fund,proof,conflicts

def features(trade):
 if len(trade)%60:raise ValueError('incomplete hour')
 a=trade.reshape(-1,60,5);ok=np.isfinite(a).all(axis=(1,2));c=a[:,-1,3].copy();h=np.max(a[:,:,1],axis=1);l=np.min(a[:,:,2],axis=1);v=np.sum(a[:,:,4],axis=1)
 c[~ok]=np.nan;v[~ok]=np.nan;returns=np.full(len(c),np.nan);returns[1:]=c[1:]/c[:-1]-1
 rows=[];invalid=0
 for i in range(168,len(c)):
  T=START+(i+1)*60*M
  if not np.isfinite(c[i-168:i+1]).all() or not np.isfinite(v[i-168:i+1]).all():invalid+=1;continue
  hist=returns[i-167:i+1];med=np.median(v[i-168:i]);sma=np.mean(c[i-23:i+1])
  if not np.isfinite(hist).all() or med<=0 or sma<=0:invalid+=1;continue
  x=[c[i]/c[i-k]-1 for k in (1,2,4,12,24)]
  x.extend([np.std(returns[i-23:i+1],ddof=1),np.std(hist,ddof=1),(h[i]-l[i])/c[i],v[i]/med-1,c[i]/sma-1])
  hour=dt.datetime.fromtimestamp(T/1000,dt.timezone.utc).hour;x.extend([math.sin(2*math.pi*hour/24),math.cos(2*math.pi*hour/24)])
  first=execution.ix(T+M);last=execution.ix(T+61*M);target=None
  if 0<=first<len(trade) and 0<=last<len(trade) and np.isfinite(trade[first,0]) and np.isfinite(trade[last,0]):target=float(trade[last,0]/trade[first,0]-1)
  rows.append({'T':T,'x':[float(q) for q in x],'target':target,'target_end_ms':T+61*M})
 return rows,invalid

def month_shift(year,month,delta):
 total=year*12+month-1+delta;return total//12,total%12+1
def month_ms(year,month):return int(dt.datetime(year,month,1,tzinfo=dt.timezone.utc).timestamp()*1000)
def month_name(t):return dt.datetime.fromtimestamp(t/1000,dt.timezone.utc).strftime('%Y-%m')

def split_fold(rows,y,m):
 a=month_ms(y,m);ny,nm=month_shift(y,m,1);b=month_ms(ny,nm);ty,tm=month_shift(y,m,-6);start=month_ms(ty,tm)
 train=[r for r in rows if start<=r['T']<a and r['target'] is not None and r['target_end_ms']<a]
 test=[r for r in rows if a<=r['T']<b]
 return start,a,b,train,test

def train_predict(rows,symbol):
 predictions=[];folds=[]
 for idx in range(16):
  y,m=month_shift(2025,7,idx);start,a,b,train,test=split_fold(rows,y,m)
  if not train or not test:raise ValueError('missing required training/test month')
  X=np.array([r['x'] for r in train]);target=np.array([r['target'] for r in train]);Xtest=np.array([r['x'] for r in test])
  xgb=XGBRegressor(n_estimators=200,max_depth=3,learning_rate=.03,min_child_weight=20,subsample=1,colsample_bytree=1,reg_lambda=10,objective='reg:squarederror',random_state=42,n_jobs=2)
  xgb.fit(X,target);p1=xgb.predict(Xtest)
  mean=X.mean(axis=0);scale=X.std(axis=0);scale[scale==0]=1
  ridge=Ridge(alpha=10);ridge.fit((X-mean)/scale,target);p2=ridge.predict((Xtest-mean)/scale)
  folds.append({'symbol':symbol,'test_month':f'{y:04d}-{m:02d}','train_start_ms':start,'test_start_ms':a,'test_end_ms':b,'last_training_target_end_ms':max(r['target_end_ms'] for r in train),'train_rows':len(train),'test_rows':len(test)})
  for model,values in (('XGBoost',p1),('Ridge',p2)):
   for r,p in zip(test,values):
    predictions.append({'symbol':symbol,'model':model,'fold':f'{y:04d}-{m:02d}','T':r['T'],'availability_ms':r['T']+1000,'train_start_ms':start,'train_end_ms':a,'last_training_target_end_ms':folds[-1]['last_training_target_end_ms'],'predicted':float(p),'actual':r['target'],'target_end_ms':r['target_end_ms']})
  print('trained',symbol,f'{y:04d}-{m:02d}',len(train),len(test),flush=True)
 return predictions,folds

def direction(p,threshold):
 return ('LONG' if p>0 else 'SHORT') if p!=0 and abs(p)>=threshold else None

def summarize(records,account):
 z=execution.summarize(records,account);trades=[r for r in records if 'net'in r]
 z['closed_long']=sum(r['direction']=='LONG' for r in trades);z['closed_short']=sum(r['direction']=='SHORT' for r in trades)
 z['cost_to_abs_gross']=(z['fees']+z['impact']+z['liquidation_fee'])/abs(z['gross']) if abs(z['gross'])>1e-12 else None
 return z

def run_account(predictions,trade,mark,fund,symbol,model,scenario,mode):
 fby={}
 for t,rate in fund:fby.setdefault(execution.ix(t-t%M),[]).append((t,rate))
 account={'cash':1000.,'peak':1000.,'max_dd':0.};busy=0;halt=False;records=[];selected=0;seen=set()
 for p in predictions:
  if p['symbol']!=symbol or p['model']!=model:continue
  d=direction(p['predicted'],THRESHOLD if mode=='threshold' else 0.)
  if d is None:continue
  selected+=1;r={'symbol':symbol,'model':model,'scenario':scenario,'mode':mode,'signal_ms':p['T'],'availability_ms':p['availability_ms'],'direction':d,'predicted':p['predicted'],'actual_target':p['actual'],'fold':p['fold']}
  if p['T'] in seen:r['rejection']='duplicate_signal'
  elif halt:r['rejection']='prior_position_unresolved'
  elif p['T']<busy:r['ignored']='position_open'
  else:
   action=execution.execute(r,trade,mark,fby,account,symbol,scenario=='stress',hold_minutes=60);r.update(action)
   if 'exit_ms'in action:busy=action['exit_ms'];r['exit_month']=month_name(action['exit_ms'])
   if 'unresolved'in action:halt=True
  seen.add(p['T']);records.append(r)
 z=summarize(records,account);z['above_threshold_or_sign']=selected
 for side in ('LONG','SHORT'):
  subset=[x for x in records if x['direction']==side];z[side]=summarize(subset,{'cash':None,'max_dd':None})
 z['months']={}
 for i in range(16):
  y,m=month_shift(2025,7,i);month=f'{y:04d}-{m:02d}';subset=[r for r in records if r.get('exit_month')==month]
  z['months'][month]=summarize(subset,{'cash':None,'max_dd':None})
 return records,z

def main(root,out):
 out.mkdir(parents=True,exist_ok=True);predictions=[];records=[];summaries={};metadata={};folds=[]
 for symbol in ('BTCUSDT','ETHUSDT'):
  trade,mark,fund,proof,conflicts=load_stream(root,symbol);rows,invalid=features(trade);pred,fd=train_predict(rows,symbol);predictions.extend(pred);folds.extend(fd)
  metadata[symbol]={'inputs':proof,'overlap_conflicts':conflicts,'trade_missing_minutes':int(np.isnan(trade[:,0]).sum()),'mark_missing_minutes':int(np.isnan(mark[:,0]).sum()),'invalid_hours':invalid,'features_rows':len(rows)}
  for model,mode in (('XGBoost','threshold'),('Ridge','threshold'),('XGBoost','sign_only')):
   for scenario in ('base','stress'):
    rec,z=run_account(pred,trade[:,:4],mark,fund,symbol,model,scenario,mode);records.extend(rec)
    eligible=[p for p in pred if p['model']==model and direction(p['predicted'],THRESHOLD if mode=='threshold' else 0.) is not None]
    observed=[p for p in eligible if p['actual'] is not None]
    z['selected_prediction_mean']=sum(p['predicted'] for p in eligible)/len(eligible) if eligible else None
    z['selected_actual_mean']=sum(p['actual'] for p in observed)/len(observed) if observed else None
    z['selected_observed_targets']=len(observed);z['selected_missing_targets']=len(eligible)-len(observed)
    summaries[f'{symbol}/{model}/{mode}/{scenario}']=z
    print('simulated',symbol,model,mode,scenario,z['closed'],flush=True)
 output={'summary':summaries,'metadata':metadata,'folds':folds,'versions':{'python':sys.version,'numpy':np.__version__,'scipy':scipy.__version__,'sklearn':sklearn.__version__,'xgboost':xgboost.__version__}}
 with gzip.open(out/'predictions.json.gz','wt',encoding='utf8',newline='\n') as f:json.dump(predictions,f,sort_keys=True,allow_nan=False)
 with gzip.open(out/'trades.json.gz','wt',encoding='utf8',newline='\n') as f:json.dump(records,f,sort_keys=True,allow_nan=False)
 (out/'summary.json').write_text(json.dumps(output,indent=2,sort_keys=True,allow_nan=False),encoding='utf8')
 print('finished',len(predictions),'predictions',len(records),'selected records',flush=True)

if __name__=='__main__':main(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]))
