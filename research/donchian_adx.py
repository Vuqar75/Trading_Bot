"""Single Wilder ADX gate and attribution against the frozen Donchian ledger."""
import json
from collections import Counter
import numpy as np
from . import donchian_4h as base

OUT=base.OUT.parent/'donchian_adx_results'
PERIOD=14
THRESHOLD=25

def wilder(values):
    result=np.full(len(values),np.nan)
    count=0
    total=0.
    average=np.nan
    for i,value in enumerate(values):
        if not np.isfinite(value):
            count=0;total=0.;average=np.nan
            continue
        if count<PERIOD:
            count+=1;total+=value
            if count==PERIOD:average=total/PERIOD
        else:
            average=(average*(PERIOD-1)+value)/PERIOD
        result[i]=average
    return result

def adx(prices):
    bars=prices[:len(prices)//base.BAR*base.BAR].reshape(-1,base.BAR,4)
    highs=bars[:,:,1].max(axis=1)
    lows=bars[:,:,2].min(axis=1)
    previous_close=bars[:-1,-1,3]
    up=np.diff(highs);down=-np.diff(lows)
    tr=np.maximum.reduce([highs[1:]-lows[1:],abs(highs[1:]-previous_close),abs(lows[1:]-previous_close)])
    plus=np.where(up>np.maximum(0,down),up,0.)
    minus=np.where(down>np.maximum(0,up),down,0.)
    valid=np.isfinite(up)&np.isfinite(down)&np.isfinite(tr)
    inputs=[np.r_[np.nan,np.where(valid,x,np.nan)] for x in (tr,plus,minus)]
    tr_s,plus_s,minus_s=map(wilder,inputs)
    plus_di=np.divide(100*plus_s,tr_s,out=np.zeros_like(tr_s),where=tr_s>0)
    minus_di=np.divide(100*minus_s,tr_s,out=np.zeros_like(tr_s),where=tr_s>0)
    denominator=plus_di+minus_di
    dx=np.divide(100*abs(plus_di-minus_di),denominator,out=np.zeros_like(tr_s),where=denominator>0)
    dx[~np.isfinite(tr_s)]=np.nan
    return wilder(dx)

def rejection(score):
    if not np.isfinite(score):return 'adx_unavailable'
    return 'adx_below_25' if score<THRESHOLD else None

def measure(old,new,start,values,nov):
    def score(ts):
        i=(ts-start)//base.M//base.BAR-1
        return float(values[i])
    def label(row):
        x=score(row['signal_ms'])
        return dict(row,adx=x if np.isfinite(x) else None)
    def identity(t):return t['signal_ms'],t['side']
    baseline={identity(t):t for t in old['trades']}
    filtered={identity(t):t for t in new['trades']}
    rejected=[label(t) for t in baseline.values() if rejection(score(t['signal_ms']))]
    deleted=[label(baseline[k]) for k in sorted(baseline.keys()-filtered.keys())]
    added=[label(filtered[k]) for k in sorted(filtered.keys()-baseline.keys())]
    common=[(baseline[k],filtered[k]) for k in sorted(baseline.keys()&filtered.keys())]
    def outcomes(rows):
        return dict(**base.metrics(rows),win=sum(t['net']>0 for t in rows),loss=sum(t['net']<0 for t in rows),
                    terminal=sum(t['reason']=='END_MTM' for t in rows))
    direct_keys={identity(t) for t in rejected}
    assert not direct_keys & filtered.keys()
    expected=base.metrics(new['trades'])['net']-base.metrics(old['trades'])['net']
    common_delta=sum(b['net']-a['net'] for a,b in common)
    assert abs(expected-(sum(t['net'] for t in added)-sum(t['net'] for t in deleted)+common_delta))<1e-9
    return dict(baseline=base.summarize(old,nov),filtered=base.summarize(new,nov),
                fixed_deleted=outcomes(rejected),fixed_retained=outcomes([t for k,t in baseline.items() if k not in direct_keys]),
                fixed_deleted_rows=rejected,added_rows=added,removed_by_occupancy_rows=[t for t in deleted if identity(t) not in direct_keys],
                added=outcomes(added),removed_by_occupancy=outcomes([t for t in deleted if identity(t) not in direct_keys]),
                common_n=len(common),common_net_delta=common_delta,net_delta=expected,
                baseline_attempt_matrix=dict(Counter(t['status']+'/'+(rejection(score(t['signal_ms'])) or 'adx_pass')
                                                     for t in old['attempts'])),
                baseline_nov_attempts=[label(t) for t in old['attempts']] if nov else [],
                filtered_nov_attempts=[label(t) for t in new['attempts']] if nov else [])

def main():
    OUT.mkdir(exist_ok=True)
    comparison={}
    for nov in (False,True):
        if nov:
            start,p,m,_,rates=base.av.load(True)
            begin=20*base.BAR
        else:
            p,m,rates,_,_=base.av.core.load_data(base.av.ARCHIVE,'BTCUSDT')
            start=base.av.core.START
            begin=base.av.core.ix(base.av.core.BEGIN)
        values=adx(p)
        for stress,suffix in enumerate(('-base','-stress')):
            name=('nov' if nov else 'history')+suffix
            dest=OUT/(name+'.json')
            if not dest.exists():
                replay=base.replay(start,p,m,rates,stress,begin,'adx-'+name,
                                   entry_gate=lambda i: rejection(values[i//base.BAR-1]))
                base.store(dest,replay)
            old=json.loads((base.OUT/(name+'.json')).read_text())
            new=json.loads(dest.read_text())
            comparison[name]=measure(old,new,start,values,nov)
            if nov:
                e=json.loads((base.av.OUT/'episode.json').read_text())
                t=e['start']+e['b']*base.M
                comparison[name]['filtered_crossing_mtm']=[
                    base.crossing_value(position,start,e['b'],p,m,rates,stress)
                    for position in new['trades'] if position['entry_ms']<=t<position['exit_ms']]
    base.store(OUT/'comparison.json',comparison)

if __name__=='__main__':main()
