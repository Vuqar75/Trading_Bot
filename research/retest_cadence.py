"""Offline single-policy frequency comparison. All indices are minutes from START UTC."""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path
import numpy as np
from research import extreme_reversal as core

OUT = Path(__file__).with_name('retest_cadence_results')
BEGIN = core.ix(core.BEGIN)

def scenarios(prices):
    pending = None
    signals = []
    for end in range(BEGIN, len(prices), 15):
        bar = prices[end-15:end]
        prev = prices[end-30:end-15]
        if not np.isfinite(prices[end-300:end]).all():
            pending = None
            continue
        high, low, close = bar[:,1].max(), bar[:,2].min(), bar[-1,3]
        if pending and end-pending['start'] > 240:
            pending = None
        if end % 60 == 0:
            prior = prices[end-241:end-1:60,3]
            side = 1 if close > max(prior) else -1 if close < min(prior) else 0
            if side and (not pending or side != pending['side']):
                hour = prices[end-60:end]
                pending = dict(start=end, side=side, level=max(prior) if side==1 else min(prior),
                               target=hour[:,1].max() if side==1 else hour[:,2].min(), touch=None)
                continue
        if not pending:
            continue
        p = pending
        s = p['side']
        if p['touch'] is None:
            if (prev[-1,3]-p['level'])*s >= 0 and low <= p['level'] <= high:
                p.update(touch=end, extreme=low if s==1 else high)
            else:
                p['target'] = max(high,p['target']) if s==1 else min(low,p['target'])
            continue
        p['extreme'] = min(low,p['extreme']) if s==1 else max(high,p['extreme'])
        if close > prev[:,1].max() if s==1 else close < prev[:,2].min():
            change = close/prices[end-1441,3]-1
            signals.append(dict(**p, at=end, stop=round(p['extreme']-s*.1,1),
                                regime='up' if change>.01 else 'down' if change<-.01 else 'range'))
            pending = None
    return signals

def simulate(signals, prices, marks, funding, slow, stress):
    impact = .001 if stress else .0005
    account = dict(cash=1000.,peak=1000.,max_dd=0.)
    busy = -1
    rows = []
    for sig in signals:
        at = sig['at']
        if slow and at%240:
            continue
        r = dict(sig)
        rows.append(r)
        i = at+(8 if stress else 3)
        deadline = i+120
        s,stop,target = sig['side'],sig['stop'],sig['target']
        if at+2 < busy:
            r['reject']='position';continue
        if deadline>=len(prices):
            r['reject']='end';continue
        before=prices[at:i]
        if not np.isfinite(before).all() or not np.isfinite([prices[i,0],marks[i,0]]).all():
            r['reject']='missing';continue
        ref=prices[i,0]
        entry=ref*(1+s*impact)
        q,err=core.size_for(entry,marks[i,0],'BTCUSDT',impact)
        if s*(entry-stop)<=0 or s*(target-entry)<=0 or before[:,1].max()>=max(target,stop) or before[:,2].min()<=min(target,stop):
            r['reject']='geometry_or_level_spent';continue
        if err or q*entry*1.0005>account['cash'] or q*(abs(stop-entry)+entry*(.0031+2*impact))>5:
            r['reject']=err or 'risk_or_cash';continue
        paid=0.
        for j in range(i,deadline+1):
            o,h,l,_=prices[j]
            mo,mh,ml,_=marks[j]
            if not np.isfinite([o,h,l,mo,mh,ml]).all():raise ValueError('missing while open')
            sl=l<=stop if s==1 else h>=stop
            tp=h>=target if s==1 else l<=target
            gap_sl=(o-stop)*s<=0
            gap_tp=(o-target)*s>=0
            why='TIME' if j==deadline else 'SL' if gap_sl else 'TP' if gap_tp else 'SL' if sl else 'TP' if tp else None
            for ts,rate in funding[j]:
                amount=-q*s*mo*rate
                if j==i or why:
                    if (gap_sl or gap_tp or j==deadline) and ts>core.START+j*core.M:continue
                    amount=min(amount,0.)
                paid+=amount
            for mp in ((mo,) if why=='TIME' else ((mh,ml) if s==1 else (ml,mh))):
                core.equity_update(account,account['cash']+paid+q*((mp-entry)*s-entry*.0005-mp*(impact+.0005)))
            if why:
                out=o if why=='TIME' or gap_sl else stop if why=='SL' else target
                exit_fill=out*(1-s*impact)
                gross=q*s*(out-ref)
                fees=q*.0005*(entry+exit_fill)
                friction=q*(abs(entry-ref)+abs(out-exit_fill))
                net=gross-fees-friction+paid
                account['cash']+=net
                core.equity_update(account,account['cash'])
                busy=j+(why!='TIME' and not gap_sl and not gap_tp)
                r.update(i=i,j=j,qty=q,entry=entry,exit=exit_fill,reason=why,ambiguous=bool(sl and tp and not gap_sl and not gap_tp and j!=deadline),
                         gross=gross,fees=fees,impact=friction,funding=paid,net=net)
                break
    assert abs(1000+sum(r.get('net',0) for r in rows)-account['cash'])<1e-8
    return dict(account=account,rows=rows)

def main(root):
    OUT.mkdir(exist_ok=True)
    prices,marks,rates,_,_=core.load_data(root,'BTCUSDT')
    funding=defaultdict(list)
    for t,v in rates:funding[(t-core.START)//core.M].append((t,v))
    signals=scenarios(prices)
    anchor=None
    example=None
    for i in range(BEGIN,len(prices)):
        if prices[i,2]<=62000<=prices[i,1]:anchor=i
        if anchor is not None and 0<i-anchor<=10080 and prices[i,2]<=54000<=prices[i,1]:
            example=dict(from_minute=anchor,to_minute=i,from_bar=prices[anchor].tolist(),to_bar=prices[i].tolist());break
    (OUT/'coverage.json').write_text(json.dumps(dict(example=example,low=float(np.nanmin(prices[BEGIN:,2])),high=float(np.nanmax(prices[BEGIN:,1])))))
    for slow in (False,True):
        for stress in (False,True):
            dest=OUT/(('4h' if slow else '15m')+('-stress' if stress else '-base')+'.json.gz')
            if dest.exists():continue
            result=simulate(signals,prices,marks,funding,slow,stress)
            tmp=dest.with_suffix('.tmp')
            tmp.write_bytes(gzip.compress(json.dumps(result,allow_nan=False).encode(),mtime=0))
            tmp.replace(dest)
if __name__=='__main__':main(Path(sys.argv[1]))
