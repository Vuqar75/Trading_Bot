"""Reprice a fixed set of ATR entries and replay either exit on a single account."""
import gzip
import json
from pathlib import Path
import numpy as np
import pandas as pd
from . import action_values as av

OUT=av.OUT.parent/'trend_exit_results'
M=av.core.M
def reversals(p):
    hours=p[:len(p)//60*60].reshape(-1,60,4)
    high=hours[:,:,1].max(axis=1);low=hours[:,:,2].min(axis=1);close=hours[:,-1,3]
    return {s:np.array([(h+1)*60 for h in range(4,len(hours))
                       if (close[h]<low[h-4:h].min() if s==1 else close[h]>high[h-4:h].max())],
                       dtype=int) for s in (1,-1)}

def deadline(at,s,delay,signals):
    entry=at+delay
    later=signals[s]
    k=later.searchsorted(entry,side='right')
    cap=entry+2880
    if k<len(later) and later[k]+delay<cap:
        return int(later[k]+delay),'REV'
    return cap,'CAP'

def sizing(r,m,stress,cash=1000):
    q,why=av.core.size_for(r['entry'],m[r['i'],0],'BTCUSDT',0)
    if why:return None,why
    if q*(abs(r['entry']-r['stop'])+r['entry']*(.0031+(0.002 if stress else 0.001)))>5:return None,'risk'
    if q*r['entry']*1.0005>cash:return None,'cash'
    return q,None

def path_metrics(r,m,s,stress,q):
    j=r['j']
    arr=m[r['i']:j+1][:,[1,2] if s==1 else [2,1]].flatten()
    if r['why'] in ('REV','CAP','TIME'):
        arr=np.r_[arr[:-2],m[j,0]]
    friction=.001 if stress else .0005
    pnl=q*(s*(arr-r['entry'])-r['entry']*.0005-arr*(friction+.0005)+min(0,r['funding']))
    pnl=np.r_[0,pnl,q*r['net']]
    return float(pnl.max()),float(pnl.min()),float(np.max(np.maximum.accumulate(pnl)-pnl))

def save(file,rows):
    tmp=file.with_suffix('.tmp')
    tmp.write_bytes(gzip.compress(json.dumps(rows,allow_nan=False,separators=(',',':')).encode(),mtime=0))
    tmp.replace(file)

def outcome_fields(r,start):
    return dict(r,entry_ms=start+r['i']*M,exit_minute_ms=start+r['j']*M)

def episode():
    start,p,m,vol,rates=av.load(True)
    ft,fr=map(np.asarray,zip(*rates))
    e=json.loads((av.OUT/'episode.json').read_text())
    pred=pd.read_csv(av.OUT/'nov-predictions.csv.gz')
    selected=pred[pred.ts.between(start+e['a']*M,start+e['b']*M)]
    assert len(selected)==130
    rows=[]
    for stress in (False,True):
        busy=-1;cash=1000.
        for record in selected.to_dict('records'):
            at=int(record['at'])
            row=dict(record,stress=stress)
            r=av.path(at,record['atr'],-1,stress,p,m,ft,fr,start)
            if r is None:
                row['reject']='missing_price_or_data'
            else:
                row['trade']=outcome_fields(r,start)
                q,reason=sizing(r,m,stress)
                row.update(q=q,reject=reason)
                if q is not None and at>=busy:
                    seq,why=sizing(r,m,stress,cash)
                    if seq is not None:
                        busy=r['until'];cash+=seq*r['net']
                        row['sequential_net']=seq*r['net']
                if r['why']=='TIME':
                    end=min(len(p),r['i']+2880)
                    after=p[r['j']:end]
                    sl=np.flatnonzero(after[:,1]>=r['stop'])
                    tp=np.flatnonzero(after[:,2]<=r['target'])
                    t_sl=int(sl[0])+r['j'] if len(sl) else None
                    t_tp=int(tp[0])+r['j'] if len(tp) else None
                    both=t_sl is not None and t_sl==t_tp
                    if both:
                        open_price=p[t_sl,0]
                        if open_price>=r['stop']:t_tp=None
                        elif open_price<=r['target']:t_sl=None
                    row['after_time']=dict(first_SL_ms=None if t_sl is None else start+t_sl*M,
                        first_TP_ms=None if t_tp is None else start+t_tp*M,observed_until_ms=start+end*M,
                        truncated=end<r['i']+2880,ambiguous=bool(both and t_sl is not None and t_tp is not None))
            rows.append(row)
    save(OUT/'episode.json.gz',rows)

def compare():
    start,p,m,_,rates=av.load(False)
    ft,fr=map(np.asarray,zip(*rates))
    signals=reversals(p)
    pred=pd.read_csv(av.OUT/'test-predictions.csv.gz')
    pred=pred[pred['at']+2886<len(p)].copy()
    pred['month']=pd.to_datetime(pred.ts,unit='ms').dt.strftime('%Y-%m')
    for month, group in pred.groupby('month'):
        file=OUT/(month+'.json.gz')
        if file.exists():continue
        rows=[]
        for record in group.to_dict('records'):
            at=int(record['at'])
            for stress in (False,True):
                delay=6 if stress else 1
                for side in (1,-1):
                    row=dict(ts=int(record['ts']),at=at,side=side,stress=stress,model=int(record['model']),
                             baseline=bool(np.sign(record['r60'])==side),regime=record['regime'])
                    old=av.path(at,record['atr'],side,stress,p,m,ft,fr,start)
                    if old is None:
                        row['reject']='missing_base_path';rows.append(row);continue
                    q,why=sizing(old,m,stress)
                    if why:
                        row['reject']=why;rows.append(row);continue
                    newer=av.path(at,record['atr'],side,stress,p,m,ft,fr,start,scheduled=deadline(at,side,delay,signals))
                    if newer is None:raise ValueError('incomplete prolonged path')
                    row['q']=q
                    for key,r in [('time',old),('trend',newer)]:
                        hi,lo,drop=path_metrics(r,m,side,stress,q)
                        row[key]=dict(**r,hi=hi,lo=lo,drop=drop)
                    rows.append(row)
        save(file,rows)

if __name__=='__main__':
    OUT.mkdir(exist_ok=True)
    if not (OUT/'episode.json.gz').exists():episode()
    compare()
