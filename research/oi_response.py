"""Fixed lagged natural-unit OI rule/control; resumable offline research."""
import hashlib
import json
import gzip
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research import rsi2_dynamic as common
from research.derivative_metrics_data import load as load_metrics
from research.technical_patterns import rma

core=common.core;M=common.M;SPAN=15*M
VARIANTS=('price_control','oi_filter')


def asof(ts,values,queries):
    ix=np.searchsorted(ts,queries,side='right')-1;safe=np.maximum(ix,0)
    ok=(ix>=0)&(queries-ts[safe]<=5*M)
    out=np.full(len(queries),np.nan);out[ok]=values[safe[ok]]
    source=np.full(len(queries),-1,dtype=np.int64);source[ok]=ts[safe[ok]]
    return out,ok,source


def geometry(entry,side,anchor,stop):
    if not all(np.isfinite(x) for x in (entry,anchor,stop)) or entry<=0:return False
    distance=side*(entry-stop)/entry;room=side*(anchor-entry)/entry
    if not 0<distance<=.02 or room<.006:return False
    def net(exit_reference):
        fill=exit_reference*(1-side*.001)
        return side*(fill-entry)-.0005*(entry+fill)
    win,loss=net(anchor),net(stop)
    return bool(win>0 and loss<0 and win>=-loss)


def indicators(trade,ts,oi,start):
    if len(trade)%15 or not np.isfinite(trade).all() or len(ts)==0:raise ValueError('signal inputs')
    a=trade.reshape(-1,15,4);o,h,l,c=a[:,0,0],a[:,:,1].max(1),a[:,:,2].min(1),a[:,-1,3]
    prev=np.r_[c[0],c[:-1]];tr=np.maximum(h-l,np.maximum(abs(h-prev),abs(l-prev)))
    atr=rma(tr,14);ema=pd.Series(c).ewm(span=20,adjust=False).mean().to_numpy()
    signal_times=start+(np.arange(len(c))+1)*SPAN;setup_end=signal_times-SPAN
    after,ok1,t1=asof(ts,oi,setup_end);before,ok0,t0=asof(ts,oi,setup_end-60*M)
    valid=ok0&ok1;drop=after/before-1
    side=np.zeros(len(c),dtype=np.int8);anchor=np.full(len(c),np.nan);stop=anchor.copy()
    for i in range(16,len(c)):
        s=i-1
        if not valid[i] or not abs(c[s]-o[s])>=1.5*atr[s-1]:continue
        direction=1 if c[s]<o[s] else -1 if c[s]>o[s] else 0
        if not direction or not (c[i]>o[i] and c[i]>c[s] if direction==1 else c[i]<o[i] and c[i]<c[s]):continue
        level=float(min(l[s],l[i]) if direction==1 else max(h[s],h[i]));target=float(ema[s-1])
        if geometry(float(c[i]*(1+direction*.001)),direction,target,level):
            side[i]=direction;anchor[i]=target;stop[i]=level
    return {'close':c,'price_control':side,'oi_filter':np.where(drop<=-.01,side,0).astype(np.int8),
            'anchor':anchor,'stop':stop,'oi_before':before,'oi_after':after,'oi_drop':drop,'oi_start_ms':t0,'oi_end_ms':t1,
            'setup_end_ms':setup_end,'anchor_asof_ms':setup_end-SPAN,'metric_valid':valid}


def account_class():
    Base,sha=common.account_class()
    class Response(Base):
        def __init__(self,symbol,stress):super().__init__(symbol,stress,15);self.contexts={}
        def enter(self,t,target,raw,mark,signal):
            source=self.contexts[signal];fill=raw*(1+target*self.impact);stop=source['stop_reference'];anchor=source['exit_anchor']
            if not geometry(fill,target,anchor,stop):
                self.events.append({'time_ms':t,'action':'reject','reason':'geometry_after_delay','signal_ms':signal});return
            super().enter(t,target,raw,mark,signal)
            if self.position:
                p=self.position;distance=target*(fill-stop)/fill
                p.update(stop=stop,stop_reference=stop,stop_pct=distance,exit_anchor=anchor,oi_end_ms=source['oi_end_ms'],oi_start_ms=source['oi_start_ms'],variant=source['variant'])
                p['estimated_risk']=p['qty']*fill*(distance+2*(self.fee+self.impact)+.0021)
    return Response,sha


def simulate(trade,mark,funding,data,symbol,variant,stress,begin,end):
    import bisect
    Account,_=account_class();a=Account(symbol,stress);slots=((np.flatnonzero(data[variant])+1)*15).tolist();fby={}
    for t,rate in funding:fby.setdefault((t-core.START)//M,[]).append((t,rate))
    i,last=(begin-core.START)//M,(end-core.START)//M;decisions=[]
    while i<last:
        if not a.position and not a.pending:
            k=bisect.bisect_left(slots,i)
            if k==len(slots) or slots[k]>=last:break
            i=slots[k]
        t=core.START+i*M
        if i%15==0:
            j=i//15-1
            if j>=0:
                if a.position and not a.pending:
                    anchor=a.position['exit_anchor'];c=float(data['close'][j]);current=a.current()
                    if (c>=anchor if current==1 else c<=anchor):
                        context={'time_ms':t,'close':c,'exit_line':anchor,'entry_side':0,'action':'mean_exit'}
                        a.signal(t,0,context);decisions.append(context)
                elif not a.position and not a.pending and data[variant][j]:
                    side=int(data[variant][j])
                    if t+(6 if stress else 1)*M+360*M>=end:a.events.append({'time_ms':t,'action':'reject','reason':'incomplete_end_window'})
                    else:
                        context={'time_ms':t,'entry_side':side,'action':'entry','variant':variant,'close':float(data['close'][j]),
                            'stop_reference':float(data['stop'][j]),'exit_anchor':float(data['anchor'][j]),'oi_drop':float(data['oi_drop'][j]),
                            'oi_end_ms':int(data['oi_end_ms'][j]),'oi_start_ms':int(data['oi_start_ms'][j]),'setup_end_ms':int(data['setup_end_ms'][j]),
                            'oi_before':float(data['oi_before'][j]),'oi_after':float(data['oi_after'][j]),'anchor_asof_ms':int(data['anchor_asof_ms'][j])}
                        a.contexts[t]=context;a.signal(t,side,context);decisions.append(context)
        if a.position or a.pending:a.tick(t,trade[i],mark[i],fby.get(i,[]))
        if a.halted:raise ValueError('account halted')
        i+=1
    result=common.summarize(a)
    result['development_quarters']={q:float(sum(p['net'] for p in a.trades if lo<=p['exit_ms']<hi)) for q,lo,hi in
        [('2025Q3',core.stamp('2025-07-01'),core.stamp('2025-10-01')),('2025Q4',core.stamp('2025-10-01'),core.stamp('2026-01-01'))]}
    return result,{'trades':a.trades,'events':a.events,'signals':a.signals,'decisions':decisions}


def eligible(base,stress,development):
    periods=stress['development_quarters'].values() if development else [stress['segments'][str(2026)+'H'+str(k)] for k in (1,2)]
    return {'counts':all(r['closed']>=30 and r['long']>=10 and r['short']>=10 for r in (base,stress)),
            'positive_both':base['net']>0 and stress['net']>0,'both_periods_positive':all(v>0 for v in periods),
            'without_best_positive':stress['without_best']>0,'drawdown':max(base['drawdown'],stress['drawdown'])<=.02}


def run(root,metrics_root,out,stage):
    if stage not in ('development','validation'):raise ValueError('unknown stage')
    project=Path(__file__).resolve().parents[1]
    for name,digest in json.loads((project/'OI_RESPONSE_INPUTS.json').read_text())['sha256'].items():
        if hashlib.sha256((project/name).read_bytes()).hexdigest()!=digest:raise ValueError('registered inputs changed: '+name)
    expected=Path(__file__).resolve().parent/'derivative_feature_results/source_manifest.json'
    if hashlib.sha256((metrics_root/'manifest.json').read_bytes()).hexdigest()!=hashlib.sha256(expected.read_bytes()).hexdigest():raise ValueError('metrics manifest changed')
    metrics,quality=load_metrics(metrics_root);out.mkdir(parents=True,exist_ok=True)
    selected=None
    if stage=='validation':
        selection=json.loads((out/'selection.json').read_text());selected=selection['selected']
        if hashlib.sha256((out/'development.json').read_bytes()).hexdigest()!=selection['development_summary_sha256']:raise ValueError('development changed')
        if selected is None:raise ValueError('no candidate; no validation scan')
    assets=common.ASSETS if stage=='development' else (selected.split('/')[0],)
    results,journals,sources,checks={},{},{},{}
    begin,end=(core.stamp('2025-07-01'),core.stamp('2026-01-01')) if stage=='development' else (core.stamp('2026-01-01'),core.END)
    signature=hashlib.sha256((Path(__file__).resolve().parents[1]/'OI_RESPONSE_INPUTS.json').read_bytes()+stage.encode()).hexdigest()
    checkpoint_path=out/(stage+'_checkpoint.json');checkpoint=json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {'signature':signature,'cases':{}}
    if checkpoint['signature']!=signature:raise ValueError('checkpoint inputs changed')
    for symbol in assets:
        trade,mark,fund,proof,conflicts=core.load_data(root,symbol)
        if conflicts or not np.isfinite(mark).all():raise ValueError('source gap/conflict')
        sources[symbol]=proof;limit=(end-core.START)//M;ts,values=metrics[symbol]
        data=indicators(trade[:limit],ts,values[:,0],core.START)
        cut=min(limit,(core.stamp('2025-08-01')-core.START)//M)
        prefix=indicators(trade[:cut],ts,values[:,0],core.START)
        for k in data:np.testing.assert_allclose(data[k][:cut//15],prefix[k],equal_nan=True)
        variants=VARIANTS if stage=='development' else (selected.split('/')[1],)
        for variant in variants:
            key=symbol+'/'+variant
            for stress in (False,True):
                case=key+('/stress' if stress else '/base');part=out/(stage+'-'+case.replace('/','-')+'.json.gz')
                if case in checkpoint['cases']:
                    if hashlib.sha256(part.read_bytes()).hexdigest()!=checkpoint['cases'][case]:raise ValueError('checkpoint part corrupted')
                    payload=json.load(gzip.open(part,'rt'));results[case],journals[case]=payload['result'],payload['journal'];print('reused',case,flush=True)
                else:
                    if part.exists():raise ValueError('orphan checkpoint preserved; explicit recovery required')
                    results[case],journals[case]=simulate(trade,mark,fund,data,symbol,variant,stress,begin,end)
                    common.write_gzip(part,{'result':results[case],'journal':journals[case]});checkpoint['cases'][case]=hashlib.sha256(part.read_bytes()).hexdigest()
                    temp=checkpoint_path.with_suffix('.tmp');temp.write_text(json.dumps(checkpoint,indent=2)+'\n');temp.replace(checkpoint_path)
                    print(case,results[case]['closed'],results[case]['net'],flush=True)
            checks[key]=eligible(results[key+'/base'],results[key+'/stress'],stage=='development')
    allowed=sorted([k for k,v in checks.items() if all(v.values())],key=lambda k:(-results[k+'/stress']['net'],-results[k+'/stress']['closed'],k))
    winner=allowed[0] if allowed else None
    common.write_gzip(out/(stage+'_journal.json.gz'),journals)
    summary={'stage':stage+('2025H2' if stage=='development' else '2026'),'results':results,'checks':checks,'selected':winner if stage=='development' else selected,
             'candidate':bool(winner) if stage=='validation' else False,'sources':sources,'metric_quality':quality,'trading_admitted':False,
             'journal_sha256':hashlib.sha256((out/(stage+'_journal.json.gz')).read_bytes()).hexdigest()}
    if stage=='validation':summary['selection_sha256']=hashlib.sha256((out/'selection.json').read_bytes()).hexdigest()
    (out/(stage+'.json')).write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    if stage=='development':(out/'selection.json').write_text(json.dumps({'selected':winner,'development_summary_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'validation_performed':False,'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'selected':summary['selected'],'passed':allowed},indent=2))


if __name__=='__main__':run(Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),sys.argv[1])
