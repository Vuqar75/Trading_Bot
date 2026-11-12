"""Fixed pooled hourly net-payoff ML; isolated files, no APIs or trading access."""
import bisect
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from research import legacy_universe_data as source
from research import legacy_vwap as legacy
from research import rsi2_dynamic as common
from research.technical_patterns import rma

core=common.core;M=common.M;H=60*M;ASSETS=source.ASSETS
FEATURES=['ret1','ret4','ret24','sma20_gap','atr14_pct','vol20','rsi14','body_range','close_location','volume_ratio',
          'market_ret1','market_ret4','market_ret24','residual_ret1','residual_ret4','rank_ret1','rank_ret24',
          'lagged_funding','funding_age_hours']+['asset_'+s for s in ASSETS]
MODELS=('HGBR','Ridge')


def features(markets,start):
    by={};endtimes=None
    for symbol in ASSETS:
        trade,mark,volume,fund=markets[symbol]
        if len(trade)%60 or not np.isfinite(trade).all():raise ValueError('hourly feature input')
        hours=trade.reshape(-1,60,4);c=hours[:,-1,3];h=hours[:,:,1].max(1);l=hours[:,:,2].min(1)
        v=volume.reshape(-1,60).sum(1);n=len(c)
        times=start+(np.arange(n)+1)*H
        if endtimes is None:endtimes=times
        elif not np.array_equal(times,endtimes):raise ValueError('crossasset time mismatch')
        returns={k:np.r_[np.full(k,np.nan),np.log(c[k:]/c[:-k])] for k in (1,4,24)}
        delta=np.r_[np.nan,np.diff(c)];u=rma(np.maximum(delta,0),14);d=rma(np.maximum(-delta,0),14)
        rsi=np.divide(100*u,u+d,out=np.full(n,50.),where=u+d>0);rsi[~np.isfinite(u+d)]=np.nan
        previous=np.r_[c[0],c[:-1]]
        atr=rma(np.maximum(h-l,np.maximum(abs(h-previous),abs(l-previous))),14)/c
        span=np.maximum(h-l,1e-12)
        histvol=pd.Series(v).shift(1).rolling(20).mean().to_numpy()
        ft=np.array([t for t,_ in fund]);rates=np.array([r for _,r in fund]);ix=np.searchsorted(ft,times-15*M,side='right')-1
        safe=np.maximum(ix,0);age=(times-ft[safe])/H
        rate=rates[safe].copy();rate[(ix<0)|(age>24)]=np.nan;age[(ix<0)|(age>24)]=np.nan
        own=np.column_stack([returns[k] for k in (1,4,24)]+[c/pd.Series(c).rolling(20).mean().to_numpy()-1,
            atr,pd.Series(returns[1]).rolling(20).std().to_numpy(),rsi,
            (c-hours[:,0,0])/span,(c-l)/span,np.divide(v,histvol,out=np.full(n,np.nan),where=histvol>0)])
        by[symbol]={'own':own,'returns':returns,'funding':rate,'age':age,'times':times,'atr':atr,
                    'funding_source_ms':np.where(ix>=0,ft[safe],-1)}
    mean={k:np.mean(np.stack([by[s]['returns'][k] for s in ASSETS]),axis=0) for k in (1,4,24)}
    ranks={k:pd.DataFrame({s:by[s]['returns'][k] for s in ASSETS}).rank(axis=1,pct=True).to_numpy() for k in (1,24)}
    for si,symbol in enumerate(ASSETS):
        a=by[symbol];onehot=np.zeros((len(endtimes),len(ASSETS)));onehot[:,si]=1
        a['X']=np.column_stack([a['own']]+[mean[k] for k in (1,4,24)]+[a['returns'][k]-mean[k] for k in (1,4)]+
            [ranks[k][:,si] for k in (1,24)]+[a['funding'],a['age'],onehot])
        if a['X'].shape[1]!=len(FEATURES):raise ValueError('feature schema')
    return by


def label(t,side,symbol,market,start):
    """Vectorized SAME fixed2%-stop/4h contract used by portfolio Account.tick."""
    trade,mark,_,fund=market;entry=t+6*M;ei=(entry-start)//M;deadline=ei+240
    if deadline>=len(trade):return None
    raw=float(trade[ei,0]);fill=raw*(1+side*.001);mp=float(mark[ei,0])
    qty,error=legacy.sizing(fill,mp,symbol,.001)
    if error or not .97<=fill/mp<=1.03:return None
    stop=fill*(1-side*.02);path=trade[ei:deadline];mpath=mark[ei:deadline]
    # Exclude a label if the simplified stop scan cannot certify no earlier
    # mark liquidation. Actual traded records still use full Account.tick.
    if np.min(mpath[:,2])<=fill*.5 or np.max(mpath[:,1])>=fill*1.5:return None
    crossed=np.flatnonzero(path[:,2]<=stop if side==1 else path[:,1]>=stop)
    if len(crossed):
        k=int(crossed[0]);exit_i=ei+k;exit_time=start+(exit_i+1)*M
        exit_raw=float(min(path[k,0],stop) if side==1 else max(path[k,0],stop));reason='stop'
    else:
        exit_i=deadline;exit_time=start+exit_i*M;exit_raw=float(trade[exit_i,0]);reason='time'
    fees=qty*.0005*(fill+exit_raw*(1-side*.001));impact=qty*(abs(fill-raw)+abs(exit_raw*.001))
    gross=side*qty*(exit_raw-raw);funding=0.;events=[]
    for ft,rate in fund[bisect.bisect_left(fund,(entry,-float('inf'))):bisect.bisect_left(fund,(exit_time,-float('inf')))]:
        mi=(ft-start)//M;amount=-side*qty*float(mark[mi,0])*rate
        ambiguous=bool(reason=='stop' and (ft>start+exit_i*M or (ft==start+exit_i*M and (path[k,0]<=stop if side==1 else path[k,0]>=stop))))
        if ambiguous:amount=min(amount,0.)
        funding+=amount;events.append({'event_ms':ft,'rate':rate,'applied':amount})
    if abs(funding)>qty*fill*.25:return None
    net=gross-fees-impact+funding
    return {'net_return':net/(qty*raw),'net':net,'qty':qty,'entry_ms':entry,'exit_ms':exit_time,'reason':reason,
            'entry':fill,'exit':exit_raw*(1-side*.001),'gross':gross,'fees':fees,'impact':impact,'funding':funding,'funding_events':events}


def split(rows,a,b):
    date=dt.datetime.fromtimestamp(a/1000,dt.timezone.utc);i=date.year*12+date.month-1-6
    lo=core.stamp(f'{i//12}-{i%12+1:02d}-01')
    train=[r for r in rows if lo<=r['T']<a and r['label_end_ms']<a]
    test=[r for r in rows if a<=r['T']<b]
    return train,test,lo


def factory(name):
    if name=='Ridge':return make_pipeline(StandardScaler(),Ridge(alpha=100))
    if name=='HGBR':return HistGradientBoostingRegressor(max_iter=100,learning_rate=.05,max_leaf_nodes=7,max_depth=3,
        min_samples_leaf=200,l2_regularization=10,early_stopping=False,random_state=20261112)
    raise ValueError('unregistered model')


def train_month(rows,a,b,out,signature):
    name=dt.datetime.fromtimestamp(a/1000,dt.timezone.utc).strftime('%Y-%m')
    receipt=out/(name+'-receipt.json');part=out/(name+'-predictions.json.gz')
    if receipt.exists():
        old=json.loads(receipt.read_text())
        if old['signature']!=signature or old['predictions_sha256']!=hashlib.sha256(part.read_bytes()).hexdigest():raise ValueError('month checkpoint changed')
        for f,h in old['models_sha256'].items():
            if hashlib.sha256((out/f).read_bytes()).hexdigest()!=h:raise ValueError('saved model changed')
        print('reused trained month',name,flush=True)
        return json.load(gzip.open(part,'rt')),old
    train,test,lo=split(rows,a,b)
    X=np.array([r['x'] for r in train]);Xt=np.array([r['x'] for r in test]);predictions=[];hashes={}
    if not len(train) or not len(test) or max(r['label_end_ms'] for r in train)>=a:raise ValueError('purged training window')
    for model in MODELS:
        estimates={}
        for side in (-1,1):
            valid=np.array([r['labels'][str(side)] is not None for r in train])
            y=np.array([r['labels'][str(side)]['net_return'] for r in train if r['labels'][str(side)] is not None])
            if len(y)<1000:raise ValueError('insufficient directional training')
            fitted=factory(model)
            with threadpool_limits(limits=1):fitted.fit(X[valid],y);estimates[side]=fitted.predict(Xt)
            model_file=name+'-'+model+'-'+str(side)+'.joblib';joblib.dump(fitted,out/model_file)
            hashes[model_file]=hashlib.sha256((out/model_file).read_bytes()).hexdigest()
        for j,row in enumerate(test):
            predictions.append({'model':model,'symbol':row['symbol'],'T':row['T'],'fold':name,
                'feature_sha256':hashlib.sha256(json.dumps(row['x'],separators=(',',':')).encode()).hexdigest(),
                'long':float(estimates[1][j]),'short':float(estimates[-1][j])})
    common.write_gzip(part,predictions)
    meta={'signature':signature,'train_start_ms':lo,'test_start_ms':a,'test_end_ms':b,
          'maximum_training_label_end_ms':max(r['label_end_ms'] for r in train),'train_rows':len(train),'test_rows':len(test),
          'predictions_sha256':hashlib.sha256(part.read_bytes()).hexdigest(),'models_sha256':hashes}
    receipt.write_text(json.dumps(meta,indent=2)+'\n')
    print('saved trained month',name,len(train),len(test),flush=True)
    return predictions,meta


def account_class():
    Base,sha=common.account_class();financial=next(c for c in Base.__mro__ if c.__name__=='Account')
    financial.enter.__globals__['core']=SimpleNamespace(size_for=legacy.sizing,funding_amount=core.funding_amount,segment=core.segment)
    class Panel(Base):
        def __init__(self,symbol,stress):super().__init__(symbol,stress,60);self.contexts={}
        def enter(self,t,target,raw,mark,signal):
            super().enter(t,target,raw,mark,signal)
            if self.position:
                self.position.update(symbol=self.symbol,deadline_ms=t+240*M,
                    predicted_stress_net=self.contexts[signal]['predicted_net'],model=self.contexts[signal]['model'])
    return Panel,sha


def candidates(predictions,model):
    out={}
    for r in predictions:
        if r['model']!=model:continue
        for side,field in ((1,'long'),(-1,'short')):
            if r[field]>=.0005:
                out.setdefault(r['T'],[]).append({'symbol':r['symbol'],'entry_side':side,'predicted_net':r[field],
                    'model':model,'fold':r['fold'],'feature_sha256':r['feature_sha256']})
    for t in out:out[t].sort(key=lambda r:(-r['predicted_net'],r['symbol'],-r['entry_side']))
    return out


def simulate(markets,predictions,model,stress,begin,end):
    Account,_=account_class();a=Account(ASSETS[0],stress);signals=candidates(predictions,model);slots=sorted(signals)
    fby={s:{} for s in ASSETS}
    for s in ASSETS:
        for t,rate in markets[s][3]:fby[s].setdefault((t-core.START)//M,[]).append((t,rate))
    t=begin;decisions=[]
    while t<end:
        if not a.position and not a.pending:
            k=bisect.bisect_left(slots,t)
            if k==len(slots):break
            t=slots[k]
            if t+246*M>=end:break
            context={**signals[t][0],'time_ms':t,'action':'entry','rank_rule':'predicted_net','candidates':signals[t]}
            a.symbol=context['symbol'];a.contexts[t]=context;a.signal(t,context['entry_side'],context);decisions.append(context)
        i=(t-core.START)//M;trade,mark,_,_=markets[a.symbol]
        if a.position or a.pending:a.tick(t,trade[i],mark[i],fby[a.symbol].get(i,[]))
        if a.halted:raise ValueError('unresolved account')
        t+=M
    result=common.summarize(a)
    result['by_asset']={s:{'closed':sum(p['symbol']==s for p in a.trades),'net':float(sum(p['net'] for p in a.trades if p['symbol']==s))} for s in ASSETS}
    result['without_best_asset']=result['net']-max(r['net'] for r in result['by_asset'].values())
    result['active_months']=len(set(dt.datetime.fromtimestamp(p['entry_ms']/1000,dt.timezone.utc).strftime('%Y-%m') for p in a.trades))
    result['quarters']={q:sum(p['net'] for p in a.trades if lo<=p['exit_ms']<hi) for q,lo,hi in
        [('2025Q3',core.stamp('2025-07-01'),core.stamp('2025-10-01')),('2025Q4',core.stamp('2025-10-01'),end)]}
    return result,{'trades':a.trades,'events':a.events,'signals':a.signals,'decisions':decisions}


def criteria(base,stress):
    return {k:bool(v) for k,v in {'counts':all(r['closed']>=30 and r['long']>=10 and r['short']>=10 for r in (base,stress)),
        'positive_both':base['net']>0 and stress['net']>0,'both_quarters_positive':all(v>0 for v in stress['quarters'].values()),
        'without_best_positive':stress['without_best']>0,'without_best_asset_positive':stress['without_best_asset']>0,
        'drawdown':max(base['drawdown'],stress['drawdown'])<=.02,'active_months':stress['active_months']>=3}.items()}


def run(root,out):
    project=Path(__file__).resolve().parents[1];receipt=json.loads((project/'PANEL_NET_ML_INPUTS.json').read_text())
    for n,h in receipt['sha256'].items():
        if hashlib.sha256((project/n).read_bytes()).hexdigest()!=h:raise ValueError('registered file changed '+n)
    if receipt['versions']!={'numpy':np.__version__,'sklearn':sklearn.__version__}:raise ValueError('model dependency drift')
    manifest=root/'manifest.json'
    if hashlib.sha256(manifest.read_bytes()).hexdigest()!=receipt['manifest_sha256']:raise ValueError('source manifest drift')
    core.START=core.stamp('2024-12-01');core.BEGIN=core.stamp('2025-01-01');core.END=core.stamp('2026-01-01');core.N=(core.END-core.START)//M
    out.mkdir(parents=True,exist_ok=True);signature=hashlib.sha256(json.dumps(receipt,sort_keys=True).encode()).hexdigest()
    markets={};proofs={}
    for s in ASSETS:
        trade,mark,volume,fund,proof=source.load(root,s,core.START,core.BEGIN,core.END)
        markets[s]=(trade,mark,volume,fund);proofs[s]=proof;print('verified input',s,flush=True)
    dataset=out/'dataset.json.gz';dreceipt=out/'dataset-receipt.json'
    if dreceipt.exists():
        meta=json.loads(dreceipt.read_text())
        if meta['signature']!=signature or meta['sha256']!=hashlib.sha256(dataset.read_bytes()).hexdigest():raise ValueError('dataset checkpoint drift')
        rows=json.load(gzip.open(dataset,'rt'));print('reused labelled dataset',len(rows),flush=True)
    else:
        data=features(markets,core.START);cut=60*250
        prefix=features({s:(a[:cut],b[:cut],c[:cut],f) for s,(a,b,c,f) in markets.items()},core.START)
        for s in ASSETS:np.testing.assert_allclose(data[s]['X'][:250],prefix[s]['X'],equal_nan=True)
        rows=[]
        for s in ASSETS:
            a=data[s]
            for j,t in enumerate(a['times']):
                t=int(t)
                if not core.BEGIN<=t<core.END-246*M or not np.isfinite(a['X'][j]).all():continue
                labels={str(side):label(t,side,s,markets[s],core.START) for side in (-1,1)}
                rows.append({'symbol':s,'T':t,'x':a['X'][j].tolist(),'label_end_ms':t+246*M,
                             'funding_source_ms':int(a['funding_source_ms'][j]),'labels':labels})
            print('labelled',s,flush=True)
        rows.sort(key=lambda r:(r['T'],r['symbol']));common.write_gzip(dataset,rows)
        dreceipt.write_text(json.dumps({'signature':signature,'sha256':hashlib.sha256(dataset.read_bytes()).hexdigest()},indent=2)+'\n')
    predictions=[];folds=[]
    for month in range(7,13):
        a=core.stamp(f'2025-{month:02d}-01');b=core.stamp(f'2025-{month+1:02d}-01') if month<12 else core.END
        pred,meta=train_month(rows,a,b,out,signature);predictions.extend(pred);folds.append(meta)
    common.write_gzip(out/'predictions.json.gz',predictions)
    results,journals,checks={},{},{}
    for model in MODELS:
        for stress in (False,True):
            key=model+('/stress' if stress else '/base');name=key.replace('/','-');part=out/(name+'.json.gz');meta=out/(name+'-receipt.json')
            if meta.exists():
                old=json.loads(meta.read_text())
                if old['signature']!=signature or old['sha256']!=hashlib.sha256(part.read_bytes()).hexdigest():raise ValueError('execution checkpoint drift')
                payload=json.load(gzip.open(part,'rt'));results[key],journals[key]=payload['result'],payload['journal']
            else:
                results[key],journals[key]=simulate(markets,predictions,model,stress,core.stamp('2025-07-01'),core.END)
                common.write_gzip(part,{'result':results[key],'journal':journals[key]})
                meta.write_text(json.dumps({'signature':signature,'sha256':hashlib.sha256(part.read_bytes()).hexdigest()},indent=2)+'\n')
            print(key,results[key]['closed'],results[key]['net'],flush=True)
        checks[model]=criteria(results[model+'/base'],results[model+'/stress'])
    allowed=[m for m in MODELS if all(checks[m].values())];allowed.sort(key=lambda m:(-results[m+'/stress']['net'],results[m+'/stress']['drawdown'],m))
    common.write_gzip(out/'development_journal.json.gz',journals)
    summary={'stage':'development2025H2','selected':allowed[0] if allowed else None,'universe':list(ASSETS),'results':results,'checks':checks,
             'trading_admitted':False,'sources':proofs,'folds':folds,'feature_names':FEATURES,
             'journal_sha256':hashlib.sha256((out/'development_journal.json.gz').read_bytes()).hexdigest()}
    (out/'development.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (out/'selection.json').write_text(json.dumps({'selected':summary['selected'],'development_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'validation_performed':False,'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'selected':summary['selected'],'checks':checks},indent=2))


if __name__=='__main__':run(Path(sys.argv[1]),Path(sys.argv[2]))
