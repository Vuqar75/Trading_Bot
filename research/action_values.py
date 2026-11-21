"""Quarterly observations, paired executable labels, frozen linear fits and one-position replay."""
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from research import extreme_reversal as core
from research.action_value_nov import ROOT as NOV

ARCHIVE=Path('D:/TradingBot-reproducible-cache-20261111/session-momentum-cache')
CACHE=Path('D:/TradingBot-ML-work/action-value-v1')
OUT=Path(__file__).with_name('action_value_results')
FEATURES=['r15','r60','r240','pullback','high_distance','low_distance','volatility','volume_ratio']
SPLIT=core.stamp('2026-01-01')
def load(nov):
    if nov:
        parts=[]
        for prefix in ('klines','markPriceKlines'):
            rows=[]
            for path in sorted(NOV.glob(prefix+'*.json')):
                raw=path.read_bytes()
                assert hashlib.sha256(raw).hexdigest()==json.loads(path.with_suffix('.meta').read_text())['sha256']
                rows.extend(json.loads(raw))
            a=np.array(rows,dtype=float)
            assert (np.diff(a[:,0])==core.M).all()
            parts.append(a)
        assert np.array_equal(parts[0][:,0],parts[1][:,0])
        rates=[(r['fundingTime'],float(r['fundingRate'])) for p in sorted(NOV.glob('funding*.json')) for r in json.loads(p.read_bytes())]
        assert all(abs(b[0]-a[0]-8*3600000)<60000 for a,b in zip(rates,rates[1:]))
        return int(parts[0][0,0]),parts[0][:,1:5],parts[1][:,1:5],parts[0][:,5],rates
    p,m,r,files,_=core.load_data(ARCHIVE,'BTCUSDT')
    vol=np.full(len(p),np.nan)
    for item in files:
        path=ARCHIVE/item['file']
        if 'klines' not in path.name and path.parent.name!='klines':continue
        rows=json.loads(path.read_bytes()) if path.suffix=='.json' else core.zipped(path)
        for row in rows:
            if str(row[0]).isdigit():
                i=core.ix(int(row[0]))
                if 0<=i<len(vol) and np.isnan(vol[i]):vol[i]=float(row[5])
    return core.START,p,m,vol,r

def observations(p,v):
    out=[]
    for at in range(1680,len(p)-246,15):
        h=at//60*60
        four=at//240*240
        past=p[at-1440:at]
        vol=v[at-1455:at].reshape(97,15).sum(axis=1)
        if not np.isfinite(past).all() or not np.isfinite(vol).all() or np.median(vol[:-1])<=0:continue
        closes=p[at-1441:at:15,3]
        c=p[at-1,3]
        recent=p[at-240:at]
        hi,lo=max(recent[:,1]),min(recent[:,2])
        if hi<=lo:continue
        hourly=p[h-840:h].reshape(14,60,4)
        hh=hourly[:,:,1].max(axis=1);ll=hourly[:,:,2].min(axis=1);pc=p[h-841:h-1:60,3]
        atr=np.maximum.reduce([hh-ll,abs(hh-pc),abs(ll-pc)]).mean()
        x=[c/p[at-15,0]-1,p[h-1,3]/p[h-60,0]-1,p[four-1,3]/p[four-240,0]-1,
           (hi-c)/(hi-lo),c/max(past[:,1])-1,c/min(past[:,2])-1,np.std(np.diff(np.log(closes)),ddof=1),vol[-1]/np.median(vol[:-1])]
        if np.isfinite(x).all() and atr>0:
            ret=c/closes[0]-1
            out.append([at,atr,'up' if ret>.01 else 'down' if ret<-.01 else 'range',*x])
    return pd.DataFrame(out,columns=['at','atr','regime',*FEATURES])

def path(at,atr,s,stress,p,m,ft,fr,start,*,scheduled=None):
    friction=.001 if stress else .0005
    i=at+(6 if stress else 1);end=i+240
    scheduled_reason='TIME'
    if scheduled is not None:
        end,scheduled_reason=scheduled
    if end>=len(p):
        return None
    bars=p[i:end]
    if not np.isfinite(p[i:end+1]).all() or not np.isfinite(m[i:end+1]).all():return None
    ref=p[i,0];entry=ref*(1+s*friction)
    lower=math.floor((entry-(atr if s==1 else 2*atr))*10)/10
    upper=math.ceil((entry+(2*atr if s==1 else atr))*10)/10
    stop,target=(lower,upper) if s==1 else (upper,lower)
    hit=np.flatnonzero((bars[:,2]<=lower)|(bars[:,1]>=upper))
    j=i+int(hit[0]) if len(hit) else end
    o,h,l,_=p[j]
    gap=o<=lower or o>=upper
    ambiguous=False
    if j==end and (scheduled is None or not gap):
        why=scheduled_reason;out=o
    else:
        ambiguous=bool(not gap and l<=lower and h>=upper)
        stopped=(s*(o-stop)<=0 if gap else (l<=stop if s==1 else h>=stop))
        why='SL' if stopped else 'TP'
        out=(min(stop,o) if s==1 else max(stop,o)) if stopped else target
    exitfill=out*(1-s*friction)
    cashflow=0.
    for k in range(ft.searchsorted(start+i*core.M),ft.searchsorted(start+(j+1)*core.M)):
        minute=(ft[k]-start)//core.M
        amount=-s*m[minute,0]*fr[k]
        if minute==j:
            if j==end or gap:continue
            amount=min(0.,amount)
        if ft[k]==start+i*core.M:amount=min(0.,amount)
        cashflow+=amount
    gross=s*(out-ref);fees=.0005*(entry+exitfill);impact=abs(entry-ref)+abs(exitfill-out)
    net=gross-fees-impact+cashflow
    return dict(i=i,j=j,until=j+int(j!=end and not gap),entry=entry,exit=exitfill,stop=stop,target=target,why=why,
                ambiguous=ambiguous,gross=gross,fees=fees,impact=impact,funding=cashflow,net=net,bp=net/entry*10000)

def evaluate(nov=False,fit=None):
    start,p,m,v,rates=load(nov)
    obs=observations(p,v)
    obs['ts']=start+obs['at']*core.M
    obs=obs.loc[obs.ts>=core.BEGIN].copy()
    ft,fr=map(np.asarray,zip(*rates))
    obs['month']=pd.to_datetime(obs.ts,unit='ms',utc=True).dt.strftime('%Y-%m')
    frames=[]
    for month,g in obs.groupby('month'):
        dest=CACHE/(month+'.pkl')
        if not dest.exists():
            rows=[]
            for row in g.to_dict('records'):
                for stress in (False,True) if month>='2026' else (False,):
                    for s in (1,-1):
                        row[f'{int(stress)}_{s}']=path(row['at'],row['atr'],s,stress,p,m,ft,fr,start)
                rows.append(row)
            tmp=dest.with_suffix('.tmp');pd.DataFrame(rows).to_pickle(tmp);tmp.replace(dest)
        frames.append(pd.read_pickle(dest))
    df=pd.concat(frames,ignore_index=True)
    if not nov:
        train=df[(df.ts+241*core.M<SPLIT)&df['0_1'].notna()&df['0_-1'].notna()]
        x=train[FEATURES].to_numpy();mu=x.mean(0);sd=x.std(0);sd[sd==0]=1
        z=(x-mu)/sd
        y=np.array([[a['bp'],b['bp']] for a,b in zip(train['0_1'],train['0_-1'])])
        intercept=y.mean(0);coef=np.linalg.solve(z.T@z+10*np.eye(z.shape[1]),z.T@(y-intercept))
        fit=mu,sd,intercept,coef
        (OUT/'fit.json').write_text(json.dumps(dict(n=len(train),last_label_deadline_ms=int(train.ts.max()+241*core.M),parameters=[a.tolist() for a in fit])))
    mu,sd,intercept,coef=fit
    test=df[df.ts>=SPLIT].copy()
    pred=(test[FEATURES].to_numpy()-mu)/sd@coef+intercept
    test['long'],test['short']=pred.T
    test['model']=np.where((pred[:,0]>0)&(pred[:,0]>pred[:,1]),1,np.where((pred[:,1]>0)&(pred[:,1]>pred[:,0]),-1,0))
    tag='nov' if nov else 'test'
    test[['ts','at','atr','regime',*FEATURES,'long','short','model']].to_csv(OUT/(tag+'-predictions.csv.gz'),index=False)
    allrows=[]
    for stress in (0,1):
        for variant in ('baseline','model'):
            busy=-1
            account=dict(cash=1000.,peak=1000.,max_dd=0.)
            for row in test.to_dict('records'):
                s=int(np.sign(row['r60'])) if variant=='baseline' else row['model']
                if not s or row['at']<busy:continue
                r=row[f'{stress}_{s}']
                if not isinstance(r,dict):continue
                q,why=core.size_for(r['entry'],m[r['i'],0],'BTCUSDT',0)
                impact=.001 if stress else .0005
                if why or q*r['entry']*1.0005>account['cash'] or q*(abs(r['entry']-r['stop'])+r['entry']*(.0031+2*impact))>5:continue
                busy=r['until']
                cash=account['cash'];before=account['max_dd']
                lo=0.;hi=0.;local_peak=0.;local_drop=0.
                for j in range(r['i'],r['j']+1):
                    mark=m[j]
                    for price in ([mark[0]] if j==r['j'] and r['why']=='TIME' else ([mark[1],mark[2]] if s==1 else [mark[2],mark[1]])):
                        delta=q*(s*(price-r['entry'])-r['entry']*.0005-price*(impact+.0005)+min(0,r['funding']))
                        core.equity_update(account,cash+delta)
                        hi=max(hi,delta);lo=min(lo,delta);local_peak=max(local_peak,delta);local_drop=max(local_drop,local_peak-delta)
                account['cash']+=q*r['net'];core.equity_update(account,account['cash'])
                allrows.append(dict(**r,ts=row['ts'],side=s,scenario=stress,variant=variant,q=q,regime=row['regime'],score=max(row['long'],row['short']),cash=account['cash'],dd=account['max_dd'],lo=lo,hi=hi,intra_drop=local_drop))
    pd.DataFrame(allrows).to_csv(OUT/(tag+'-fills.csv.gz'),index=False)
    if nov:
        first=np.flatnonzero(p[:,2]<=55555)[0]
        anchors=np.where((p[:first,2]<=62635)&(p[:first,1]>=62635))[0]
        a=int(anchors[-1]);b=int(first)
        (OUT/'episode.json').write_text(json.dumps(dict(start=start,a=a,b=b,first=p[a].tolist(),last=p[b].tolist(),
            sources=[json.loads(f.read_text()) for f in sorted(NOV.glob('*.meta'))])))
    return fit
if __name__=='__main__':
    CACHE.mkdir(exist_ok=True);OUT.mkdir(exist_ok=True)
    evaluate(True,evaluate())
