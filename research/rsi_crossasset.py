"""Bounded continuous cross-asset replay with frozen external RSI rules."""
import gzip,json,math
from decimal import Decimal,ROUND_DOWN
from pathlib import Path
import sys
import numpy as np
from research import rsi15m_verification as r

SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')


def configure():
    c=r.core;c.START,c.BEGIN,c.END=r.START,r.BEGIN,r.END;c.N=(r.END-r.START)//r.M
    c.SEGMENTS=[]
    for year in (2025,2026):
        for q in range(1,5):
            a=c.stamp(f'{year}-{3*q-2:02d}-01')
            b=c.stamp(f'{year}-{3*q+1:02d}-01') if q<4 else c.stamp(f'{year+1}-01-01')
            if a<r.END:c.SEGMENTS.append((f'{year}Q{q}',a,min(b,r.END)))


def sizing(price,mark,symbol,impact):
    if symbol=='BTCUSDT':q=.001;cap=150;minimum=50
    else:
        step=Decimal('.001' if symbol=='ETHUSDT' else '.01')
        q=float((Decimal('100')/Decimal(str(max(price,mark)))/step).to_integral_value(rounding=ROUND_DOWN)*step)
        cap=100;minimum=20 if symbol=='ETHUSDT' else 5
    if q<=0 or q*max(price,mark)>cap+1e-8:return None,'cap_or_step'
    if q*mark<minimum:return None,'min_notional'
    return q,None


def main(root,out):
    r.START=r.core.stamp('2024-12-01');r.BEGIN=r.core.stamp('2025-01-01');r.END=r.core.stamp('2026-11-01')
    r.FOMC|={20260128,20260318,20260429,20260617}
    configure();r.core.size_for=sizing
    Base,account_sha=r.load_account_class();Protected=r.protected_class(Base)
    results={};journals={};actions={};proofs={}
    for symbol in SYMBOLS:
        trade,mark,funding,proof,conflicts=r.core.load_data(root,symbol)
        assert not conflicts and np.isfinite(trade).all() and np.isfinite(mark).all()
        candidates=r.signals(trade);proofs[symbol]=proof
        assert candidates[:320]==r.signals(trade[:4800])
        fby={}
        for t,rate in funding:fby.setdefault((t-r.START)//r.M,[]).append((t,rate))
        for stress in (False,True):
            a=Protected(stress);a.symbol=symbol;key=symbol+('_stress' if stress else '_base')
            for i in range((r.BEGIN-r.START)//r.M,len(trade)):
                t=r.START+i*r.M
                if i%15==0:
                    sig=candidates[i//15-1];side=sig['side']
                    if side and not(a.last_exit_side==side and sig['bar_index']-a.last_exit_bar<6):
                        a.pending_source=sig;a.signal(t,side,None)
                if a.position or a.pending:a.tick(t,trade[i],mark[i],fby.get(i,[]))
                assert not a.halted
            x=a.finish(float(mark[-1,3]));results[key]=x;journals[key]=a.trades;actions[key]=a.events
            assert math.isclose(a.cash,1000+sum(z['net'] for z in a.trades)
                                -(a.position['entry_fee'] if a.position else 0)
                                +(a.position['funding'] if a.position else 0),abs_tol=1e-7)
            print(key,'closed',x['closed'],'net',x['net_including_open'],flush=True)
    out.mkdir(parents=True,exist_ok=True)
    (out/'summary.json').write_text(json.dumps({'protocol':'RSI_CROSSASSET_PROTOCOL.md','account_sha256':account_sha,'inputs':proofs,'results':results},indent=2)+'\n')
    for name,value in [('trades',journals),('actions',actions)]:
        with gzip.open(out/(name+'.json.gz'),'wt') as f:json.dump(value,f,sort_keys=True)

if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))
