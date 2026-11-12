"""Two fixed closed-bar range hypotheses with source-specific exits/stops."""
import bisect
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research import rsi2_dynamic as common

core=common.core;M=common.M
FAMILIES=('nr7','soup')


def indicators(trade,minutes):
    if minutes not in common.HOLDS or len(trade)%minutes or not np.isfinite(trade).all():raise ValueError('signal inputs')
    a=trade.reshape(-1,minutes,4);h=a[:,:,1].max(1);l=a[:,:,2].min(1);c=a[:,-1,3]
    ranges=h-l;prior6=pd.Series(ranges).shift(1).rolling(6).min().to_numpy();nr=ranges<prior6
    sma200=pd.Series(c).rolling(200,min_periods=200).mean().to_numpy()
    ema5=pd.Series(c).ewm(span=5,adjust=False).mean().to_numpy();ema10=pd.Series(c).ewm(span=10,adjust=False).mean().to_numpy()
    side={f:np.zeros(len(c),dtype=np.int8) for f in FAMILIES};trigger={f:np.full(len(c),np.nan) for f in FAMILIES};stop={f:np.full(len(c),np.nan) for f in FAMILIES}
    ageh=np.full(len(c),np.nan);agel=ageh.copy()
    for i in range(200,len(c)):
        if nr[i-1]:
            if c[i]>h[i-1] and c[i]>sma200[i]:side['nr7'][i]=1;trigger['nr7'][i]=h[i-1];stop['nr7'][i]=l[i-1]
            elif c[i]<l[i-1] and c[i]<sma200[i]:side['nr7'][i]=-1;trigger['nr7'][i]=l[i-1];stop['nr7'][i]=h[i-1]
        high=float(h[i-20:i].max());low=float(l[i-20:i].min())
        ageh[i]=20-int(np.flatnonzero(h[i-20:i]==high)[-1]);agel[i]=20-int(np.flatnonzero(l[i-20:i]==low)[-1])
        long=l[i]<low and c[i]>low and agel[i]>=4;short=h[i]>high and c[i]<high and ageh[i]>=4
        if long!=short:
            side['soup'][i]=1 if long else -1;trigger['soup'][i]=low if long else high;stop['soup'][i]=l[i] if long else h[i]
    return {'close':c,'sma200':sma200,'ema5':ema5,'ema10':ema10,'nr7_side':side['nr7'],'soup_side':side['soup'],
            'nr7_trigger':trigger['nr7'],'soup_trigger':trigger['soup'],'nr7_stop':stop['nr7'],'soup_stop':stop['soup'],
            'age_high':ageh,'age_low':agel}


def account_class():
    Base,sha=common.account_class()
    class PatternAccount(Base):
        def __init__(self,symbol,stress,minutes,family):
            super().__init__(symbol,stress,minutes);self.family=family;self.contexts={}
        def enter(self,t,target,raw,mark,signal):
            source=self.contexts[signal];fill=raw*(1+target*self.impact);stop=source['stop_reference']
            distance=target*(fill-stop)/fill
            reason='trigger_lost_after_delay' if target*(raw-source['trigger_reference'])<=0 else 'stop_distance_invalid' if not 0<distance<=.02 else None
            if reason:self.events.append({'time_ms':t,'action':'reject','reason':reason,'signal_ms':signal});return
            super().enter(t,target,raw,mark,signal)
            if self.position:
                p=self.position;p.update(stop=stop,stop_reference=stop,stop_pct=distance,trigger_reference=source['trigger_reference'],family=self.family)
                p['estimated_risk']=p['qty']*fill*(distance+2*(self.fee+self.impact)+.0021)
        def close(self,t,raw,reason,liqrate=0.):
            super().close(t,raw,'trend_exit' if reason=='invalid_signal' and self.family=='nr7' else reason,liqrate)
    return PatternAccount,sha


def simulate(trade,mark,funding,data,symbol,minutes,family,stress,begin,end):
    Account,_=account_class();a=Account(symbol,stress,minutes,family);entry=data[family+'_side']
    slots=((np.flatnonzero(entry)+1)*minutes).tolist();fby={}
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
                side=int(entry[j]);line=float(data['ema10' if family=='nr7' else 'ema5'][j])
                context={'time_ms':t,'close':float(data['close'][j]),'exit_line':line,'entry_side':side,'family':family}
                if a.position and not a.pending:
                    current=a.current();exit_now=(data['close'][j]<line if current==1 else data['close'][j]>line) if family=='nr7' else (data['close'][j]>line if current==1 else data['close'][j]<line)
                    if exit_now:context['action']='mean_exit' if family=='soup' else 'trend_exit';a.signal(t,0,context);decisions.append(context)
                    elif side:decisions.append({**context,'action':'ignored_position_open'})
                elif not a.position and not a.pending and side:
                    if t+(6 if stress else 1)*M+common.HOLDS[minutes]*M>=end:a.events.append({'time_ms':t,'action':'reject','reason':'incomplete_end_window'})
                    else:
                        context.update(action='entry',trigger_reference=float(data[family+'_trigger'][j]),stop_reference=float(data[family+'_stop'][j]))
                        a.contexts[t]=context;a.signal(t,side,context);decisions.append(context)
        if a.position or a.pending:a.tick(t,trade[i],mark[i],fby.get(i,[]))
        if a.halted:raise ValueError('account halted')
        i+=1
    return common.summarize(a),{'trades':a.trades,'events':a.events,'signals':a.signals,'decisions':decisions}


def development(root,out):
    results,journals,sources,checks={},{},{},{};end=core.stamp('2026-01-01')
    for symbol in common.ASSETS:
        trade,mark,fund,proof,conflicts=core.load_data(root,symbol)
        if conflicts or not np.isfinite(mark).all():raise ValueError('source gap/conflict')
        sources[symbol]=proof;limit=(end-core.START)//M
        for minutes in common.HOLDS:
            data=indicators(trade[:limit],minutes);prefix=indicators(trade[:minutes*400],minutes)
            for k in data:np.testing.assert_allclose(data[k][:400],prefix[k],equal_nan=True)
            for family in FAMILIES:
                key=symbol+'/'+str(minutes)+'/'+family
                for stress in (False,True):
                    case=key+('/stress' if stress else '/base')
                    results[case],journals[case]=simulate(trade,mark,fund,data,symbol,minutes,family,stress,core.BEGIN,end)
                    print(case,results[case]['closed'],results[case]['net'],flush=True)
                checks[key]=common.eligible(results[key+'/base'],results[key+'/stress'],2025)
    allowed=sorted([k for k,v in checks.items() if all(v.values())],key=lambda k:(-results[k+'/stress']['net'],-results[k+'/stress']['closed'],k))
    selected=allowed[0] if allowed else None;out.mkdir(parents=True,exist_ok=True);common.write_gzip(out/'development_journal.json.gz',journals)
    summary={'stage':'development2025','results':results,'checks':checks,'selected':selected,'sources':sources,'validation_performed':False,'trading_admitted':False,
             'journal_sha256':hashlib.sha256((out/'development_journal.json.gz').read_bytes()).hexdigest()}
    (out/'development.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (out/'selection.json').write_text(json.dumps({'selected':selected,'development_summary_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'validation_performed':False,'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'selected':selected,'passed':[k for k,v in checks.items() if all(v.values())]},indent=2))


def validation(root,out):
    selection=json.loads((out/'selection.json').read_text())
    if hashlib.sha256((out/'development.json').read_bytes()).hexdigest()!=selection['development_summary_sha256']:raise ValueError('development changed')
    if selection['selected'] is None:raise ValueError('no candidate; no validation scan')
    symbol,minutes,family=selection['selected'].split('/');minutes=int(minutes)
    trade,mark,fund,proof,conflicts=core.load_data(root,symbol)
    if conflicts:raise ValueError('source conflict')
    data=indicators(trade,minutes);results,journals={},{}
    for stress in (False,True):
        case='stress' if stress else 'base';results[case],journals[case]=simulate(trade,mark,fund,data,symbol,minutes,family,stress,core.stamp('2026-01-01'),core.END)
        print('validation',selection['selected'],case,results[case]['closed'],results[case]['net'],flush=True)
    checks=common.eligible(results['base'],results['stress'],2026);common.write_gzip(out/'validation_journal.json.gz',journals)
    summary={'stage':'validation2026','selected':selection['selected'],'results':results,'checks':checks,'candidate':all(checks.values()),'trading_admitted':False,'sources':proof,
             'selection_sha256':hashlib.sha256((out/'selection.json').read_bytes()).hexdigest(),'journal_sha256':hashlib.sha256((out/'validation_journal.json.gz').read_bytes()).hexdigest()}
    (out/'validation.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n');print(json.dumps({'candidate':summary['candidate'],'checks':checks},indent=2))


if __name__=='__main__':{'development':development,'validation':validation}[sys.argv[1]](Path(sys.argv[2]),Path(sys.argv[3]))
