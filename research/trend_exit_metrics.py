"""Summarize independent outcomes and replay actual one-position choices."""
import gzip
import json
from collections import Counter, defaultdict
import pandas as pd
from .trend_exit import OUT

def read(file):
    return json.loads(gzip.decompress(file.read_bytes()))

def totals(rows,key):
    keys=('gross','fees','impact','funding','net')
    sums={k:sum(r['q']*r[key][k] for r in rows) for k in keys}
    assert abs(sums['gross']-sums['fees']-sums['impact']+sums['funding']-sums['net'])<1e-7
    return dict(n=len(rows),**sums,positive=sum(r[key]['net']>0 for r in rows),
                exits=dict(Counter(r[key]['why'] for r in rows)),without_best=sums['net']-max((r[key]['net']*r['q'] for r in rows),default=0))

def sequence(rows,key):
    cash=peak=1000.;dd=0.;busy=-1
    fills=[]
    for r in rows:
        if not r['baseline'] or r['at']<busy:continue
        path=r[key]
        if r['q']*path['entry']*1.0005>cash:continue
        busy=path['until'];fills.append(r)
        dd=max(dd,1-(cash+path['lo'])/peak,path['drop']/(cash+path['lo']+path['drop']))
        peak=max(peak,cash+path['hi'])
        cash+=r['q']*path['net']
    return fills,dd*100

def stamp(ts):return pd.Timestamp(ts,unit='ms',tz='UTC').tz_convert('Asia/Baku').isoformat()

def main():
    months=[OUT/f'2026-{i:02d}.json.gz' for i in range(1,11)]
    rows=[r for p in months for r in read(p)]
    episode=read(OUT/'episode.json.gz')
    summary={}
    for stress in (False,True):
        all_rows=[r for r in rows if r['stress']==stress]
        valid=[r for r in all_rows if 'q' in r]
        result={'rejections':dict(Counter(r['reject'] for r in all_rows if 'reject' in r))}
        for key in ('time','trend'):
            result['pair_'+key]=totals(valid,key)
        delta=defaultdict(float)
        ignored=[r for r in valid if r['model']!=r['side']]
        result['model_not_selected']={k:totals(ignored,k) for k in ('time','trend')}
        for r in valid:
            delta[r['time']['why']+'→'+r['trend']['why']]+=r['q']*(r['trend']['net']-r['time']['net'])
        result['delta_by_exit']=dict(delta)
        ids={}
        for key in ('time','trend'):
            fills,dd=sequence(valid,key)
            ids[key]={(r['ts'],r['side']):r['q']*r[key]['net'] for r in fills}
            result['seq_'+key]=dict(**totals(fills,key),dd_pct=dd,
                side={s:totals([r for r in fills if r['side']==s],key) for s in (1,-1)},
                regime={s:totals([r for r in fills if r['regime']==s],key) for s in ('up','down','range')},
                periods={f'{a:02d}-{b:02d}':totals([r for r in fills if a<=pd.Timestamp(r['ts'],unit='ms').month<=b],key)
                    for a,b in ((1,3),(4,6),(7,10))})
        x,y=ids['time'],ids['trend']
        result['decomposition']=dict(common=len(x.keys()&y.keys()),common_delta=sum(y[i]-x[i] for i in x.keys()&y.keys()),
            time_only_n=len(x.keys()-y.keys()),time_only_net=sum(x[i] for i in x.keys()-y.keys()),
            trend_only_n=len(y.keys()-x.keys()),trend_only_net=sum(y[i] for i in y.keys()-x.keys()))
        nov=[r for r in episode if r['stress']==stress]
        allowed=[r for r in nov if r.get('q') is not None]
        positives=[r for r in allowed if r['trade']['net']>0]
        after=Counter()
        for r in allowed:
            if 'after_time' not in r:continue
            a=r['after_time'];sl=a['first_SL_ms'];tp=a['first_TP_ms']
            outcome='ambiguous' if a['ambiguous'] else 'SL_first' if sl is not None and (tp is None or sl<tp) else 'TP_first' if tp is not None else 'unobserved' if a['truncated'] else 'neither_48h'
            after[outcome]+=1
        result['episode']=dict(allowed=totals(allowed,'trade'),rejected=dict(Counter(r['reject'] for r in nov if r['reject'])),after_TIME=dict(after),
            first_positive=positives[0] if positives else None,seq_n=sum('sequential_net' in r for r in allowed),
            seq_net=sum(r.get('sequential_net',0) for r in allowed))
        summary['stress' if stress else 'base']=result
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
if __name__=='__main__':main()
