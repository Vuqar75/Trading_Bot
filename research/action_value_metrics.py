"""Reconcile net and compare predictions on the frozen final sample."""
import json
import numpy as np
import pandas as pd
from research.action_values import OUT,FEATURES,core

def stats(f):
    result={'n':len(f)}
    for key in ['gross','fees','impact','funding','net']:
        result[key]=float((f.q*f[key]).sum())
    assert abs(result['gross']-result['fees']-result['impact']+result['funding']-result['net'])<1e-7
    result['without_best']=result['net']-float((f.q*f.net).max()) if len(f) else 0
    peak=cash=1000.;dd=0
    for r in f.itertuples():
        dd=max(dd,1-(cash+r.lo)/peak,r.intra_drop/(cash+r.lo+r.intra_drop))
        peak=max(peak,cash+r.hi)
        cash+=r.q*r.net
        dd=max(dd,1-cash/peak)
    result['attributed_dd_pct']=100*dd
    return result

def main():
    f=pd.read_csv(OUT/'test-fills.csv.gz');pred=pd.read_csv(OUT/'test-predictions.csv.gz')
    f['month']=pd.to_datetime(f.ts,unit='ms',utc=True).dt.strftime('%Y-%m')
    result={}
    for (variant,stress),g in f.groupby(['variant','scenario']):
        r=stats(g)
        assert abs(r['net']+1000-g.cash.iloc[-1])<1e-6
        assert (g.i.to_numpy()[1:]>=g.until.to_numpy()[:-1]).all()
        r['dd_pct']=100*g.dd.max()
        r['regimes']={k:stats(v) for k,v in g.groupby('regime')}
        r['sides']={str(k):stats(v) for k,v in g.groupby('side')}
        r['months']={k:stats(v) for k,v in g.groupby('month')}
        if variant=='model' and not stress:
            r['quartiles']=[dict(n=len(v),forecast_bp=v.score.mean(),realized_bp=v.bp.mean()) for _,v in g.groupby(pd.qcut(g.score,4),observed=True)]
        result[variant+str(stress)]=r
    for stress in [0,1]:
        subset=f[f.scenario==stress].copy()
        subset['week']=pd.to_datetime(subset.ts,unit='ms').dt.to_period('W-SUN')
        subset['pnl']=subset.q*subset.net
        weekly=subset.pivot_table(index='week',columns='variant',values='pnl',aggfunc='sum').reindex(pd.period_range('2026-01-01','2026-10-31',freq='W-SUN')).fillna(0.)
        delta=weekly.get('model',0)-weekly['baseline']
        boot=np.random.default_rng(42).choice(delta,size=(2000,len(delta))).sum(axis=1)
        result['weekly_diff_interval'+str(stress)]=np.quantile(boot,[.025,.975]).tolist()
    result['choices']=pred.model.value_counts().to_dict()
    e=json.loads((OUT/'episode.json').read_text())
    nf=pd.read_csv(OUT/'nov-fills.csv.gz');npred=pd.read_csv(OUT/'nov-predictions.csv.gz')
    a=e['start']+e['a']*core.M;b=e['start']+e['b']*core.M
    seg=npred[npred.ts.between(a,b)]
    trades=nf[(nf.variant=='model')&(nf.scenario==0)&nf.ts.between(a,b)]
    shorts=trades[trades.side==-1]
    result['case']=dict(signals=seg.model.value_counts().to_dict(),executed=stats(trades),from_ms=a,to_ms=b)
    if len(shorts):
        row=shorts.iloc[0];result['case']['first_short']=row.to_dict()
        snapshot=seg[seg.ts==row.ts].iloc[0]
    else:
        snapshot=seg.loc[seg.short.idxmax()]
    result['case']['snapshot']=snapshot.to_dict()
    (OUT/'summary.json').write_text(json.dumps(result,indent=2,default=float))
if __name__=='__main__':main()
