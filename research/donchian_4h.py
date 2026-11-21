"""Preregistered 20/10 channels with isolated size limits and minute execution."""
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np
import pandas as pd
from . import action_values as av
from .trend_exit import sizing

OUT=av.OUT.parent/'donchian_4h_results'
BAR=240
M=av.core.M

def signals(p):
    bars=p[:len(p)//BAR*BAR].reshape(-1,BAR,4)
    high=bars[:,:,1].max(axis=1);low=bars[:,:,2].min(axis=1);close=bars[:,-1,3]
    tr=np.maximum.reduce([high[1:]-low[1:],abs(high[1:]-close[:-1]),abs(low[1:]-close[:-1])])
    for j in range(20,len(bars)):
        at=(j+1)*BAR
        if not np.isfinite(high[j-20:j+1]+low[j-20:j+1]+close[j-20:j+1]).all():
            yield at,None
            continue
        c=close[j]
        side=1 if c>max(high[j-20:j]) else -1 if c<min(low[j-20:j]) else 0
        exits=[s for s,yes in [(1,c<min(low[j-10:j])),(-1,c>max(high[j-10:j]))] if yes]
        r=c/close[j-6]-1
        yield at,dict(side=side,exits=exits,atr=float(tr[j-14:j].mean()),close=float(c),
                      low20=float(min(low[j-20:j])),high20=float(max(high[j-20:j])),
                      regime='up' if r>.01 else 'down' if r<-.01 else 'range')

def store(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2,allow_nan=False))
    tmp.replace(path)

def replay(start,p,m,rates,stress,begin,name,*,entry_gate=None):
    delay=6 if stress else 1
    impact=.001 if stress else .0005
    events=defaultdict(list)
    for ts,rate in rates:
        events[(ts-start)//M].append((ts,rate))
    candles=dict(signals(p))
    account=dict(cash=1000.,peak=1000.,max_dd=0.)
    trades=[];attempts=[];position=None;pending=None;i=begin
    month_boundaries={int((t.value//1000000-start)//M):str(t.date()) for t in
                      pd.date_range(pd.Timestamp(start,unit='ms'),pd.Timestamp(start+len(p)*M,unit='ms'),freq='MS')}
    checkpoints={}
    while i<len(p):
        ts=start+i*M
        if i in month_boundaries:
            month=month_boundaries[i]
            checkpoints[month]=dict(cash=account['cash'],account=account.copy(),trade_count=len(trades),position=None if position is None else position.copy(),pending=None if pending is None else pending.copy())
            store(OUT/(name+'-checkpoint.json'),checkpoints)
        if i in candles:
            bar=candles[i]
            if bar is None and position:raise ValueError('missing bars with open risk')
            if bar and position and position['side'] in bar['exits']:
                pending=dict(i=i+delay,kind='channel')
            elif bar and not position and bar['side'] and i+delay<len(p):
                reason=entry_gate(i) if entry_gate is not None else None
                if reason is None:
                    pending=dict(i=i+delay,kind='entry',**bar,signal_ms=ts)
                else:
                    attempts.append(dict(bar,signal_ms=ts,status=reason))
        if position is None and pending is None:
            i=min(len(p),(i//BAR+1)*BAR)
            continue
        o,h,l,c=map(float,p[i]);mo,mh,ml,mc=map(float,m[i])
        if pending and pending['i']==i and pending['kind']=='entry':
            r=dict(pending)
            pending=None
            if not np.isfinite([o,mo]).all():
                attempts.append(dict(r,status='missing_price',available_ms=ts))
                i+=1;continue
            s=r['side']
            entry=o*(1+s*impact)
            stop=(math.floor((entry-2*r['atr'])*10) if s==1 else math.ceil((entry+2*r['atr'])*10))/10
            q,reason=sizing(dict(entry=entry,stop=stop,i=i),m,stress,account['cash'])
            attempts.append(dict(r,status=reason or 'filled',available_ms=ts,reference=o,mark=mo,entry=entry,stop=stop))
            if q is not None:
                position=dict(signal_ms=r['signal_ms'],entry_ms=ts,entry=entry,stop=stop,reference=o,q=q,side=s,
                              regime=r['regime'],funding=0.,funding_n=0)
        if position:
            if not np.isfinite([o,h,l,c,mo,mh,ml,mc]).all():raise ValueError('missing minute while open')
            s=position['side'];q=position['q'];entry=position['entry'];stop=position['stop']
            opening_gap=s*(o-stop)<=0
            channel=bool(pending and pending['i']==i)
            hit=l<=stop if s==1 else h>=stop
            reason='SL' if opening_gap else 'CHANNEL' if channel else 'SL' if hit else 'END_MTM' if i==len(p)-1 else None
            immediate=opening_gap or channel
            for event,rate in events[i]:
                if immediate and event>ts:continue
                amount=-s*q*mo*rate
                if reason=='SL' or (position['entry_ms']==ts and event==ts) or (immediate and event==ts):
                    amount=min(amount,0.)
                position['funding']+=amount
                position['funding_n']+=1
            marks=[mo] if immediate else [mh,ml] if s==1 else [ml,mh]
            for px in marks:
                av.core.equity_update(account,account['cash']+position['funding']+q*(s*(px-entry)-entry*.0005-px*(impact+.0005)))
            if reason:
                ref=o if immediate else stop if reason=='SL' else c
                exit_fill=ref*(1-s*impact)
                gross=q*s*(ref-position['reference'])
                fees=q*.0005*(entry+exit_fill)
                friction=q*(abs(entry-position['reference'])+abs(exit_fill-ref))
                net=gross-fees-friction+position['funding']
                account['cash']+=net
                av.core.equity_update(account,account['cash'])
                trades.append(dict(position,exit_ms=ts,exit=exit_fill,reason=reason,gross=gross,fees=fees,impact=friction,net=net))
                position=None;pending=None
        i+=1
    assert abs(account['cash']-1000-sum(t['net'] for t in trades))<1e-8
    return dict(account=account,trades=trades,attempts=attempts,months=checkpoints)

def metrics(trades):
    return dict(n=len(trades),**{k:sum(t[k] for t in trades) for k in ('gross','fees','impact','funding','net')})

def crossing_value(position, start, minute, trade, mark, rates, stress):
    ts=start+minute*M
    q=position['q']
    side=position['side']
    impact=.001 if stress else .0005
    ref=float(trade[minute,3])
    fill=ref*(1-side*impact)
    funding=sum(-side*q*float(mark[(event-start)//M,0])*rate for event,rate in rates
                if position['entry_ms']<=event<ts+M)
    return dict(minute_ms=ts,reference=ref,modeled_exit=fill,funding=funding,
                net=q*(side*(fill-position['entry'])-.0005*(fill+position['entry']))+funding)

def summarize(run,nov):
    trades=run['trades'];closed=[t for t in trades if t['reason']!='END_MTM']
    out=dict(**metrics(trades),closed=len(closed),dd=100*run['account']['max_dd'],
             without_best=sum(t['net'] for t in trades)-max((t['net'] for t in closed),default=0),
             exits=dict(Counter(t['reason'] for t in trades)),attempts=dict(Counter(a['status'] for a in run['attempts'])),
             sides={s:metrics([t for t in trades if t['side']==s]) for s in (1,-1)},
             closed_sides={s:sum(t['side']==s for t in closed) for s in (1,-1)},
             regimes={s:metrics([t for t in trades if t['regime']==s]) for s in ('up','down','range')},
             years={y:metrics([t for t in trades if pd.Timestamp(t['signal_ms'],unit='ms').year==y]) for y in (2025,2026)})
    if nov:
        e=json.loads((av.OUT/'episode.json').read_text())
        a=e['start']+e['a']*M;b=e['start']+e['b']*M
        out['episode_attempts']=[r for r in run['attempts'] if a<=r['signal_ms']<=b]
        out['episode_positions']=[t for t in trades if t['entry_ms']<=b and t['exit_ms']>=a]
    return out

def main():
    OUT.mkdir(exist_ok=True)
    results={}
    for nov in (False,True):
        tags=[('nov' if nov else 'history')+suffix for suffix in ('-base','-stress')]
        if not all((OUT/(tag+'.json')).exists() for tag in tags):
            if nov:
                start,p,m,_,rates=av.load(True)
                begin=20*BAR
            else:
                p,m,rates,_,_=av.core.load_data(av.ARCHIVE,'BTCUSDT')
                start=av.core.START;begin=av.core.ix(av.core.BEGIN)
            for stress,tag in enumerate(tags):
                dest=OUT/(tag+'.json')
                if not dest.exists():store(dest,replay(start,p,m,rates,stress,begin,tag))
        for tag in tags:
            results[tag]=summarize(json.loads((OUT/(tag+'.json')).read_text()),nov)
        if nov:
            start,p,m,_,rates=av.load(True)
            e=json.loads((av.OUT/'episode.json').read_text())
            minute=(e['start']+e['b']*M-start)//M
            for stress,tag in enumerate(tags):
                results[tag]['crossing_mtm']=[
                    crossing_value(t,start,minute,p,m,rates,stress)
                    for t in results[tag]['episode_positions']
                    if t['entry_ms']<=start+minute*M<t['exit_ms']]
    store(OUT/'summary.json',results)

if __name__=='__main__':main()
