"""Causal standing stop-entry model, with explicitly uncertain minute fill time."""
import bisect
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research import rsi2_dynamic as common
from research.technical_patterns import rma

core=common.core;M=common.M


def indicators(trade,minutes):
    if minutes not in common.HOLDS or len(trade)%minutes or not np.isfinite(trade).all():raise ValueError('signal inputs')
    a=trade.reshape(-1,minutes,4);h=a[:,:,1].max(1);l=a[:,:,2].min(1);c=a[:,-1,3]
    width=h-l;nr=width<pd.Series(width).shift(1).rolling(6).min().to_numpy()
    sma=pd.Series(c).rolling(200,min_periods=200).mean().to_numpy();ema=pd.Series(c).ewm(span=10,adjust=False).mean().to_numpy()
    prev=np.r_[c[0],c[:-1]];tr=np.maximum(h-l,np.maximum(abs(h-prev),abs(l-prev)))
    atr=np.r_[np.nan,rma(tr,14)[:-1]]/c
    side=np.zeros(len(c),dtype=np.int8);valid=nr&(atr>=.006)&np.isfinite(sma)
    side[valid&(c>sma)]=1;side[valid&(c<sma)]=-1
    return {'close':c,'high':h,'low':l,'sma200':sma,'ema10':ema,'prior_atr_pct':atr,'side':side}


def account_class():
    Base,sha=common.account_class()
    class Conditional(Base):
        def __init__(self,symbol,stress,minutes):super().__init__(symbol,stress,minutes);self.contexts={}
        def enter(self,t,target,raw,mark,signal):
            source=self.contexts[signal];fill=raw*(1+target*self.impact);stop=source['stop_reference'];distance=target*(fill-stop)/fill
            if not source['activation_ms']<=t<source['expiry_ms']:
                self.events.append({'time_ms':t,'action':'reject','reason':'order_inactive','signal_ms':signal});return
            if not 0<distance<=.02:
                self.events.append({'time_ms':t,'action':'reject','reason':'stop_distance_invalid','signal_ms':signal});return
            super().enter(t,target,raw,mark,signal)
            if self.position:
                p=self.position;p.update(stop=stop,stop_reference=stop,stop_pct=distance,trigger_reference=source['trigger_reference'],
                    activation_ms=source['activation_ms'],order_expiry_ms=source['expiry_ms'],entry_window_end=t+M,entry_timestamp_is_interval_start=True)
                p['estimated_risk']=p['qty']*fill*(distance+2*(self.fee+self.impact)+.0021)
        def close(self,t,raw,reason,liqrate=0.):super().close(t,raw,'trend_exit' if reason=='invalid_signal' else reason,liqrate)
    return Conditional,sha


def trigger_reference(order,t,bar):
    if not order['activation_ms']<=t<order['expiry_ms']:return None
    o,h,l,_=bar;side=order['entry_side'];trigger=order['trigger_reference']
    if side==1 and h>trigger:return float(max(o,trigger))
    if side==-1 and l<trigger:return float(min(o,trigger))
    return None


def entry_minute_funding(account,t,mark,events):
    p=account.position
    for ft,rate in events:
        raw=-p['sign']*p['qty']*float(mark[0])*rate;amount=min(raw,0.)
        p['funding']+=amount;account.cash+=amount
        p['funding_events'].append({'event_ms':ft,'rate':rate,'mark_minute_open':float(mark[0]),
                                   'applied_usdt':float(amount),'ambiguous':True,'entry_time_uncertain':True})


def simulate(trade,mark,funding,data,symbol,minutes,stress,begin,end):
    Account,_=account_class();a=Account(symbol,stress,minutes);delay=(6 if stress else 1)*M
    slots=((np.flatnonzero(data['side'])+1)*minutes).tolist();fby={}
    for t,rate in funding:fby.setdefault((t-core.START)//M,[]).append((t,rate))
    i,last=(begin-core.START)//M,(end-core.START)//M;order=None;decisions=[]
    while i<last:
        if not a.position and not a.pending and order is None:
            k=bisect.bisect_left(slots,i)
            if k==len(slots) or slots[k]>=last:break
            i=slots[k]
        t=core.START+i*M
        if order and t>=order['expiry_ms']:
            a.events.append({'time_ms':t,'action':'cancel_order','reason':'expired','signal_ms':order['time_ms']});order=None
        if i%minutes==0:
            j=i//minutes-1
            if j>=0:
                if a.position and not a.pending:
                    side=a.current();line=float(data['ema10'][j]);c=float(data['close'][j])
                    if (c<line if side==1 else c>line):
                        context={'time_ms':t,'close':c,'exit_line':line,'action':'trend_exit','entry_side':0}
                        a.signal(t,0,context);decisions.append(context)
                elif not a.position and not a.pending and data['side'][j]:
                    side=int(data['side'][j])
                    if t+minutes*M+common.HOLDS[minutes]*M>=end:
                        a.events.append({'time_ms':t,'action':'reject','reason':'incomplete_end_window'})
                    else:
                        order={'time_ms':t,'entry_side':side,'action':'entry','order_type':'stop_market',
                            'trigger_reference':float(data['high'][j] if side==1 else data['low'][j]),
                            'stop_reference':float(data['low'][j] if side==1 else data['high'][j]),
                            'activation_ms':t+delay,'expiry_ms':t+minutes*M,'prior_atr_pct':float(data['prior_atr_pct'][j]),
                            'close':float(data['close'][j]),'sma200':float(data['sma200'][j])}
                        a.contexts[t]=order;a.signals.append({'time_ms':t,'desired':side,'selected_pair':order,'position':0,'action':'scheduled_conditional'});decisions.append(order)
        # Existing positions/queued mean exits observe the bar first. A newly
        # triggered order then gets pessimistic entry-before-stop processing.
        if a.position or a.pending:a.tick(t,trade[i],mark[i],fby.get(i,[]))
        if not a.position and not a.pending and order:
            ref=trigger_reference(order,t,trade[i])
            if ref is not None:
                source=order;order=None;a.enter(t,source['entry_side'],ref,float(mark[i,0]),source['time_ms'])
                if a.position:
                    a.position['entry_bar_ohlc']=[float(x) for x in trade[i]]
                    entry_minute_funding(a,t,mark[i],fby.get(i,[]))
                    a.tick(t,trade[i],mark[i],[])
        if a.halted:raise ValueError('account halted')
        i+=1
    if order:a.events.append({'time_ms':end,'action':'cancel_order','reason':'end','signal_ms':order['time_ms']})
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
            key=symbol+'/'+str(minutes)
            for stress in (False,True):
                case=key+('/stress' if stress else '/base');results[case],journals[case]=simulate(trade,mark,fund,data,symbol,minutes,stress,core.BEGIN,end)
                print(case,results[case]['closed'],results[case]['net'],flush=True)
            checks[key]=common.eligible(results[key+'/base'],results[key+'/stress'],2025)
    allowed=sorted([k for k,v in checks.items() if all(v.values())],key=lambda k:(-results[k+'/stress']['net'],-results[k+'/stress']['closed'],k));selected=allowed[0] if allowed else None
    out.mkdir(parents=True,exist_ok=True);common.write_gzip(out/'development_journal.json.gz',journals)
    summary={'stage':'development2025','results':results,'checks':checks,'selected':selected,'sources':sources,'validation_performed':False,'trading_admitted':False,
             'journal_sha256':hashlib.sha256((out/'development_journal.json.gz').read_bytes()).hexdigest()}
    (out/'development.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (out/'selection.json').write_text(json.dumps({'selected':selected,'development_summary_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'validation_performed':False,'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'selected':selected,'passed':[k for k,v in checks.items() if all(v.values())]},indent=2))


def validation(root,out):
    selection=json.loads((out/'selection.json').read_text())
    if hashlib.sha256((out/'development.json').read_bytes()).hexdigest()!=selection['development_summary_sha256']:raise ValueError('development changed')
    if selection['selected'] is None:raise ValueError('no candidate; no validation scan')
    symbol,minutes=selection['selected'].split('/');minutes=int(minutes)
    trade,mark,fund,proof,conflicts=core.load_data(root,symbol)
    if conflicts:raise ValueError('source conflict')
    data=indicators(trade,minutes);results,journals={},{}
    for stress in (False,True):
        case='stress' if stress else 'base';results[case],journals[case]=simulate(trade,mark,fund,data,symbol,minutes,stress,core.stamp('2026-01-01'),core.END)
        print('validation',selection['selected'],case,results[case]['closed'],results[case]['net'],flush=True)
    checks=common.eligible(results['base'],results['stress'],2026);common.write_gzip(out/'validation_journal.json.gz',journals)
    summary={'stage':'validation2026','selected':selection['selected'],'results':results,'checks':checks,'candidate':all(checks.values()),'trading_admitted':False,'sources':proof,
             'selection_sha256':hashlib.sha256((out/'selection.json').read_bytes()).hexdigest(),'journal_sha256':hashlib.sha256((out/'validation_journal.json.gz').read_bytes()).hexdigest()}
    (out/'validation.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n');print(json.dumps({'candidate':summary['candidate'],'checks':checks},indent=2))


if __name__=='__main__':{'development':development,'validation':validation}[sys.argv[1]](Path(sys.argv[2]),Path(sys.argv[3]))
