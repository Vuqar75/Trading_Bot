"""Frozen small-account retest adaptation. No foreign code execution."""
import gzip
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
from research import rsi15m_verification as r
from research.usdm_intraday_data import zipped


def features(trade, volume):
    a=trade.reshape(-1,15,4)
    c=a[:,-1,3];h=a[:,:,1].max(1);l=a[:,:,2].min(1)
    v=volume.reshape(-1,15).sum(1)
    def ema(x,n):return pd.Series(x).ewm(span=n,adjust=False).mean().to_numpy()
    fast,slow=ema(c,9),ema(c,15)
    prev=np.r_[c[0],c[:-1]]
    tr=np.maximum(h-l,np.maximum(abs(h-prev),abs(l-prev)))
    up=np.r_[0,np.diff(h)];dn=np.r_[0,-np.diff(l)]
    plus=r.rma(np.where((up>dn)&(up>0),up,0),14)
    minus=r.rma(np.where((dn>up)&(dn>0),dn,0),14)
    with np.errstate(invalid='ignore',divide='ignore'):
        dx=100*abs(plus-minus)/(plus+minus)
    adx=r.rma(dx,14)
    macroclose=c.reshape(-1,16)[:,-1]
    # Source shifts the macro feature by one open-labeled4h row.
    def aligned(n):return np.repeat(np.r_[np.nan,ema(macroclose,n)[:-1]],16)
    m100,m200=aligned(100),aligned(200)
    vs=pd.Series(v).rolling(50,min_periods=1).mean().to_numpy()
    pending=0;cross=-100;volume_ok=False;consumed=False
    rows=[]
    for i in range(len(c)):
        bull=i>=16 and c[i]>m100[i] and c[i]>m200[i] and m100[i]>m100[i-16]
        bear=i>=16 and c[i]<m100[i] and c[i]<m200[i] and m100[i]<m100[i-16]
        upcross=i>0 and fast[i]>slow[i] and fast[i-1]<=slow[i-1]
        dncross=i>0 and fast[i]<slow[i] and fast[i-1]>=slow[i-1]
        if upcross:
            pending=0
            if bull:pending,cross,volume_ok,consumed=1,i,v[i]>2*vs[i],False
        elif dncross:
            pending=0
            if bear:pending,cross,volume_ok,consumed=-1,i,v[i]>2*vs[i],False
        if i-cross>20:pending=0
        valid=(l[i]<=fast[i] and c[i]>slow[i]) if pending==1 else (h[i]>=fast[i] and c[i]<slow[i])
        side=pending if pending and not consumed and volume_ok and valid and adx[i]>=20 and abs(c[i]-fast[i])/fast[i]<=.01 else 0
        # Consumption occurs in account loop only if flat when signal is used.
        rows.append({'bar_index':i,'time_ms':r.START+(i+1)*15*r.M,'side':int(side),'cross':cross,
                     'stop_pct':.01,'tp_pct':.015})
    return rows


def main(root,out):
    r.configure_period()
    Base,_=r.load_account_class();Protected=r.protected_class(Base)
    trade,mark,funding,proof,conflicts=r.core.load_data(root,'BTCUSDT')
    assert not conflicts and np.isfinite(trade).all() and np.isfinite(mark).all()
    volume=np.full(len(trade),np.nan)
    for p in sorted((root/'minute/klines').glob('BTCUSDT-1m-*.zip')):
        for row in zipped(p):
            if row[0].isdigit():
                i=(int(row[0])-r.START)//r.M
                if 0<=i<len(volume):volume[i]=float(row[5])
    assert np.isfinite(volume).all() and (volume>=0).all()
    sigs=features(trade,volume)
    # Causality probe on200 completed bars, preserving full4h blocks.
    n=320*15
    assert sigs[:320]==features(trade[:n],volume[:n])
    fby={}
    for t,rate in funding:fby.setdefault((t-r.START)//r.M,[]).append((t,rate))
    results={};trades={};actions={}
    for stress in (False,True):
        a=Protected(stress);consumed=set();name='stress' if stress else 'base'
        for i in range((r.BEGIN-r.START)//r.M,len(trade)):
            t=r.START+i*r.M
            if i%15==0:
                s=sigs[i//15-1]
                if a.position and not a.pending and t-a.position['entry_ms']>=100*15*r.M:
                    a.signal(t,0,None)
                elif not a.position and not a.pending and s['side'] and s['cross'] not in consumed:
                    a.pending_source=s;a.signal(t,s['side'],None);consumed.add(s['cross'])
            if a.position or a.pending:a.tick(t,trade[i],mark[i],fby.get(i,[]))
            assert not a.halted
        results[name]=a.finish(float(mark[-1,3]));trades[name]=a.trades;actions[name]=a.events
        assert abs(a.cash-1000-sum(x['net'] for x in a.trades)
                   +(a.position['entry_fee']-a.position['funding'] if a.position else 0))<1e-7
        print(name,'closed',len(a.trades),'net',results[name]['net_including_open'],flush=True)
    out.mkdir(exist_ok=True,parents=True)
    (out/'summary.json').write_text(json.dumps({'protocol':'RETEST15M_PROTOCOL.md','inputs':proof,'checks':['causal truncation','cash reconciliation'],'results':results},indent=2)+'\n')
    for name,value in [('trades',trades),('actions',actions),('raw_signals',[s for s in sigs if s['side'] and r.BEGIN<=s['time_ms']<r.END])]:
        with gzip.open(out/(name+'.json.gz'),'wt') as f:json.dump(value,f,sort_keys=True)

if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))
