"""UTC session mean re-entry; fixed independent adaptation, no foreign import."""
import bisect
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research import rsi2_dynamic as common
from research import taker_absorption as volume_source
from research.technical_patterns import rma

M = common.M
core = common.core
FRAMES = (15, 60)


def indicators(trade, volume, minutes, start):
    if minutes not in FRAMES or len(trade)%minutes or len(volume)!=len(trade) or not np.isfinite(trade).all() or not np.isfinite(volume).all() or (volume<0).any():
        raise ValueError('signal inputs')
    a = trade.reshape(-1, minutes, 4)
    o,h,l,c = a[:,0,0],a[:,:,1].max(1),a[:,:,2].min(1),a[:,-1,3]
    v = volume.reshape(-1,minutes).sum(1); tp=(h+l+c)/3
    day=(start//M+np.arange(len(c))*minutes)//1440
    vwap=np.full(len(c),np.nan);sigma=vwap.copy()
    for d in np.unique(day):
        ix=np.flatnonzero(day==d);w=v[ix];den=np.cumsum(w)
        mu=np.divide(np.cumsum(tp[ix]*w),den,out=np.full(len(ix),np.nan),where=den>0)
        second=np.divide(np.cumsum(tp[ix]**2*w),den,out=np.full(len(ix),np.nan),where=den>0)
        vwap[ix]=mu;sigma[ix]=np.sqrt(np.maximum(0,second-mu**2))
    previous=np.r_[c[0],c[:-1]]
    tr=np.maximum(h-l,np.maximum(abs(h-previous),abs(l-previous)))
    atr=pd.Series(tr).rolling(14,min_periods=14).mean().to_numpy()
    delta=np.r_[np.nan,np.diff(c)];up=rma(np.maximum(delta,0),14);down=rma(np.maximum(-delta,0),14)
    total=up+down;rsi=np.divide(100*up,total,out=np.full(len(c),50.),where=total>0);rsi[~np.isfinite(total)]=np.nan
    lower,upper=vwap-2*sigma,vwap+2*sigma
    long=(l<=lower)&(c>lower)&(c>o)&(c-o>.5*atr)&(rsi<40)&((vwap-c)/c>=.006)
    short=(h>=upper)&(c<upper)&(c<o)&(o-c>.5*atr)&(rsi>60)&((c-vwap)/c>=.006)
    clock=(start//M+(np.arange(len(c))+1)*minutes)%1440
    allowed=(clock>=240)&(clock<1080)&(sigma>0)
    side=np.zeros(len(c),dtype=np.int8)
    side[long&~np.r_[False,long[:-1]]&allowed]=1
    side[short&~np.r_[False,short[:-1]]&allowed]=-1
    return {'close':c,'vwap':vwap,'sigma':sigma,'rsi14':rsi,'atr14':atr,'side':side}


def account_class():
    Base,sha=common.account_class()
    class VWAPAccount(Base):
        def __init__(self,symbol,stress,minutes):
            super().__init__(symbol,stress,minutes);self.contexts={}
        def enter(self,t,target,raw,mark,signal):
            source=self.contexts[signal];fill=raw*(1+target*self.impact)
            if target*(source['vwap']-fill)/fill<.006:
                self.events.append({'time_ms':t,'action':'reject','reason':'vwap_distance_after_delay','signal_ms':signal});return
            super().enter(t,target,raw,mark,signal)
            if self.position:
                self.position['deadline_ms']=t+360*M
                self.position['signal_vwap']=source['vwap']
    return VWAPAccount,sha


def simulate(trade,mark,funding,data,symbol,minutes,stress,begin,end):
    Account,_=account_class();a=Account(symbol,stress,minutes)
    slots=((np.flatnonzero(data['side'])+1)*minutes).tolist();fby={}
    for t,rate in funding:fby.setdefault((t-core.START)//M,[]).append((t,rate))
    i,last=(begin-core.START)//M,(end-core.START)//M;decisions=[]
    while i<last:
        if not a.position and not a.pending:
            k=bisect.bisect_left(slots,i)
            if k==len(slots) or slots[k]>=last:break
            i=slots[k]
        t=core.START+i*M
        if i%minutes==0:
            j=i//minutes-1
            if j>=0:
                side=int(data['side'][j])
                context={'time_ms':t,'close':float(data['close'][j]),'vwap':float(data['vwap'][j]),'exit_line':float(data['vwap'][j]),
                         'sigma':float(data['sigma'][j]),'rsi14':float(data['rsi14'][j]),'entry_side':side}
                if a.position and not a.pending:
                    exit_reached=(data['close'][j]>data['vwap'][j]) if a.current()==1 else (data['close'][j]<data['vwap'][j])
                    if exit_reached:
                        context['action']='mean_exit';a.signal(t,0,context);decisions.append(context)
                    elif side:decisions.append({**context,'action':'ignored_position_open'})
                elif not a.position and not a.pending and side:
                    if t+(6 if stress else 1)*M+360*M>=end:
                        a.events.append({'time_ms':t,'action':'reject','reason':'incomplete_end_window'})
                    else:
                        context['action']='entry';a.contexts[t]=context;a.signal(t,side,context);decisions.append(context)
        if a.position or a.pending:a.tick(t,trade[i],mark[i],fby.get(i,[]))
        if a.halted:raise ValueError('account halted')
        i+=1
    return common.summarize(a),{'trades':a.trades,'events':a.events,'signals':a.signals,'decisions':decisions}


def development(root,out):
    results,journals,sources,checks={},{},{},{};end=core.stamp('2026-01-01')
    for symbol in common.ASSETS:
        trade,mark,fund,proof,conflicts=core.load_data(root,symbol)
        if conflicts or not np.isfinite(mark).all():raise ValueError('source gap/conflict')
        volume,_=volume_source.volumes(root,proof,len(trade),core.START);sources[symbol]=proof
        limit=(end-core.START)//M
        for minutes in FRAMES:
            data=indicators(trade[:limit],volume[:limit],minutes,core.START)
            prefix=indicators(trade[:minutes*400],volume[:minutes*400],minutes,core.START)
            for k in data:np.testing.assert_allclose(data[k][:400],prefix[k],equal_nan=True)
            key=symbol+'/'+str(minutes)
            for stress in (False,True):
                case=key+('/stress' if stress else '/base')
                results[case],journals[case]=simulate(trade,mark,fund,data,symbol,minutes,stress,core.BEGIN,end)
                print(case,results[case]['closed'],results[case]['net'],flush=True)
            checks[key]=common.eligible(results[key+'/base'],results[key+'/stress'],2025)
    allowed=sorted([k for k,v in checks.items() if all(v.values())],key=lambda k:(-results[k+'/stress']['net'],-results[k+'/stress']['closed'],k))
    selected=allowed[0] if allowed else None;out.mkdir(parents=True,exist_ok=True)
    common.write_gzip(out/'development_journal.json.gz',journals)
    summary={'stage':'development2025','results':results,'checks':checks,'selected':selected,'sources':sources,'validation_performed':False,'trading_admitted':False,
             'journal_sha256':hashlib.sha256((out/'development_journal.json.gz').read_bytes()).hexdigest()}
    (out/'development.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (out/'selection.json').write_text(json.dumps({'selected':selected,'development_summary_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'validation_performed':False,'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'selected':selected,'checks':checks},indent=2))


def validation(root,out):
    selection=json.loads((out/'selection.json').read_text())
    if hashlib.sha256((out/'development.json').read_bytes()).hexdigest()!=selection['development_summary_sha256']:raise ValueError('development changed')
    if selection['selected'] is None:raise ValueError('no candidate; no validation scan')
    symbol,minutes=selection['selected'].split('/');minutes=int(minutes)
    trade,mark,fund,proof,conflicts=core.load_data(root,symbol)
    if conflicts:raise ValueError('source conflict')
    volume,_=volume_source.volumes(root,proof,len(trade),core.START);data=indicators(trade,volume,minutes,core.START)
    results,journals={},{}
    for stress in (False,True):
        case='stress' if stress else 'base'
        results[case],journals[case]=simulate(trade,mark,fund,data,symbol,minutes,stress,core.stamp('2026-01-01'),core.END)
        print('validation',selection['selected'],case,results[case]['closed'],results[case]['net'],flush=True)
    checks=common.eligible(results['base'],results['stress'],2026);common.write_gzip(out/'validation_journal.json.gz',journals)
    summary={'stage':'validation2026','selected':selection['selected'],'results':results,'checks':checks,'candidate':all(checks.values()),'trading_admitted':False,'sources':proof,
             'selection_sha256':hashlib.sha256((out/'selection.json').read_bytes()).hexdigest(),'journal_sha256':hashlib.sha256((out/'validation_journal.json.gz').read_bytes()).hexdigest()}
    (out/'validation.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n');print(json.dumps({'candidate':summary['candidate'],'checks':checks},indent=2))


if __name__=='__main__':
    {'development':development,'validation':validation}[sys.argv[1]](Path(sys.argv[2]),Path(sys.argv[3]))
