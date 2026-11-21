"""Reconcile both account paths by causal confirmation timestamp."""
import gzip
import json
from collections import Counter
from .retest_cadence import OUT
from . import extreme_reversal as c

def summarize():
    groups={}
    for path in sorted(OUT.glob('*.gz')):
        d=json.loads(gzip.decompress(path.read_bytes()))
        rows=d['rows']
        fills=[r for r in rows if 'net' in r]
        assert all(a['j']<=b['i'] for a,b in zip(fills,fills[1:]))
        assert all(r['start']<r['touch']<r['at']<r['i'] and (r['target']-r['entry'])*r['side']>0 and (r['entry']-r['stop'])*r['side']>0 for r in fills)
        assert all(abs(r['gross']+r['funding']-r['fees']-r['impact']-r['net'])<1e-9 for r in fills)
        def total(rs):return {'n':len(rs),'net':round(sum(r['net'] for r in rs),6)}
        groups[path.name.split('.')[0]]=dict(signals=len(rows),fills=total(fills),long=total([r for r in fills if r['side']==1]),short=total([r for r in fills if r['side']==-1]),
            regime={k:total([r for r in fills if r['regime']==k]) for k in ('up','down','range')},
            year={y:total([r for r in fills if c.segment(c.START+r['at']*c.M).startswith(y)]) for y in ('2025','2026')},
            costs={k:sum(r[k] for r in fills) for k in ('gross','fees','impact','funding')},dd_percent=100*d['account']['max_dd'],
            without_best=sum(r['net'] for r in fills)-max(r['net'] for r in fills),ambiguous=sum(r['ambiguous'] for r in fills),
            rejected=dict(Counter(r['reject'] for r in rows if 'reject' in r)),ids={r['at']:r['net'] for r in fills})
    for scenario in ('base','stress'):
        fast=groups['15m-'+scenario]['ids'];slow=groups['4h-'+scenario]['ids']
        assert all(abs(fast[t]-slow[t])<1e-9 for t in fast.keys() & slow.keys())
        for name,x,y in [('15m_only',fast,slow),('4h_only',slow,fast)]:
            ids=x.keys()-y.keys()
            groups[name+'-'+scenario]=dict(n=len(ids),net=sum(x[i] for i in ids))
    for g in groups.values():g.pop('ids',None)
    (OUT/'summary.json').write_text(json.dumps(groups,indent=2)+'\n')
if __name__=='__main__':summarize()
