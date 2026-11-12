"""One shared-capital, one-position portfolio of an unchanged VWAP signal."""
import bisect
import datetime as dt
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

from research import legacy_universe_data as source
from research import rsi2_dynamic as common
from research import vwap_dynamic as signal

core=common.core;M=common.M;ASSETS=source.ASSETS


def sizing(price,mark,symbol,impact):
    if symbol not in ASSETS:raise ValueError('unregistered legacy asset')
    p=Decimal(str(max(price,mark)));step=Decimal(10 if p<=10 else 1)
    q=float((Decimal(100)/p/step).to_integral_value(rounding=ROUND_DOWN)*step)
    if q<=0 or q*max(price,mark)>100+1e-8:return None,'strategy_granularity_or_cap'
    if q*mark<50-1e-8:return None,'minimum_notional_proxy50'
    return q,None


def account_class():
    Base,sha=signal.account_class()
    financial=next(cls for cls in Base.__mro__ if cls.__name__=='Account')
    financial.enter.__globals__['core']=SimpleNamespace(size_for=sizing,funding_amount=core.funding_amount,segment=core.segment)
    class Portfolio(Base):
        def enter(self,t,target,raw,mark,signal_ms):
            super().enter(t,target,raw,mark,signal_ms)
            if self.position:self.position['symbol']=self.symbol
    return Portfolio,sha


def choose(data,j):
    candidates=[]
    for symbol in ASSETS:
        d=data[symbol];side=int(d['side'][j])
        if not side:continue
        candidates.append({'symbol':symbol,'entry_side':side,'close':float(d['close'][j]),'vwap':float(d['vwap'][j]),
                           'room':float(abs(d['vwap'][j]/d['close'][j]-1))})
    candidates.sort(key=lambda r:(-r['room'],r['symbol']))
    return candidates


def simulate(markets,data,stress,begin,end):
    Account,_=account_class();a=Account(ASSETS[0],stress,60)
    slots=sorted(set(int((i+1)*60) for symbol in ASSETS for i in np.flatnonzero(data[symbol]['side'])))
    fby={}
    for symbol in ASSETS:
        fby[symbol]={}
        for t,rate in markets[symbol][3]:fby[symbol].setdefault((t-core.START)//M,[]).append((t,rate))
    i,last=(begin-core.START)//M,(end-core.START)//M;decisions=[]
    while i<last:
        if not a.position and not a.pending:
            k=bisect.bisect_left(slots,i)
            if k==len(slots) or slots[k]>=last:break
            i=slots[k]
        t=core.START+i*M
        if a.position and a.position['symbol']!=a.symbol:raise ValueError('open asset switched')
        if i%60==0:
            j=i//60-1
            if j>=0:
                if a.position and not a.pending:
                    d=data[a.symbol];c=float(d['close'][j]);line=float(d['vwap'][j]);side=a.current()
                    if (c>line if side==1 else c<line):
                        context={'time_ms':t,'symbol':a.symbol,'close':c,'exit_line':line,'action':'mean_exit','entry_side':0}
                        a.signal(t,0,context);decisions.append(context)
                    else:
                        ignored=choose(data,j)
                        if ignored:decisions.append({'time_ms':t,'action':'ignored_global_position','candidates':ignored})
                elif not a.position and not a.pending:
                    candidates=choose(data,j)
                    if candidates:
                        picked=candidates[0]
                        if t+(6 if stress else 1)*M+360*M>=end:a.events.append({'time_ms':t,'action':'reject','reason':'incomplete_end_window'})
                        else:
                            a.symbol=picked['symbol']
                            context={**picked,'time_ms':t,'action':'entry','candidates':candidates}
                            a.contexts[t]=context;a.signal(t,picked['entry_side'],context);decisions.append(context)
        if a.position or a.pending:
            trade,mark,_,_=markets[a.symbol]
            a.tick(t,trade[i],mark[i],fby[a.symbol].get(i,[]))
        if a.halted:raise ValueError('portfolio halted')
        i+=1
    result=common.summarize(a)
    result['by_asset']={s:{'closed':sum(p['symbol']==s for p in a.trades),'net':float(sum(p['net'] for p in a.trades if p['symbol']==s))} for s in ASSETS}
    result['without_best_asset']=result['net']-max(v['net'] for v in result['by_asset'].values())
    result['active_months']=len(set(dt.datetime.fromtimestamp(p['entry_ms']/1000,dt.timezone.utc).strftime('%Y-%m') for p in a.trades))
    result['max_concurrent_positions']=1 if a.trades else 0
    return result,{'trades':a.trades,'events':a.events,'signals':a.signals,'decisions':decisions}


def eligible(base,stress,year):
    checks=common.eligible(base,stress,year)
    checks.update(without_best_asset_positive=stress['without_best_asset']>0,active_months=stress['active_months']>=6)
    return checks


def run(root,out,stage):
    if stage not in ('development','validation'):raise ValueError('unknown stage')
    project=Path(__file__).resolve().parents[1];receipt=json.loads((project/'LEGACY_VWAP_EXECUTION_INPUTS.json').read_text())
    for name,digest in receipt['sha256'].items():
        if hashlib.sha256((project/name).read_bytes()).hexdigest()!=digest:raise ValueError('registered code changed: '+name)
    start=core.stamp('2024-12-01') if stage=='development' else core.stamp('2025-12-01')
    begin=core.stamp('2025-01-01') if stage=='development' else core.stamp('2026-01-01')
    end=core.stamp('2026-01-01') if stage=='development' else core.stamp('2026-11-01')
    selection=None
    if stage=='validation':
        selection=json.loads((out/'selection.json').read_text())
        if not selection['candidate'] or hashlib.sha256((out/'development.json').read_bytes()).hexdigest()!=selection['development_summary_sha256']:raise ValueError('development not passed or changed')
    core.START=start;core.BEGIN=begin;core.END=end;core.N=(end-start)//M
    markets,data,proofs={},{},{}
    for symbol in ASSETS:
        trade,mark,volume,fund,proof=source.load(root,symbol,start,begin,end)
        markets[symbol]=(trade,mark,volume,fund);proofs[symbol]=proof
        data[symbol]=signal.indicators(trade,volume,60,start)
        prefix=signal.indicators(trade[:60*400],volume[:60*400],60,start)
        for key in data[symbol]:np.testing.assert_allclose(data[symbol][key][:400],prefix[key],equal_nan=True)
        print('verified',symbol,len(proof),'sources',flush=True)
    out.mkdir(parents=True,exist_ok=True)
    manifest=root/'manifest.json';manifest_digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
    if stage=='development' and manifest_digest!=receipt['development_manifest_sha256']:raise ValueError('development source manifest changed')
    results,journals={},{}
    for stress in (False,True):
        key='stress' if stress else 'base';part=out/(stage+'-'+key+'.json.gz');meta=out/(stage+'-'+key+'-receipt.json')
        signature=hashlib.sha256(json.dumps({'stage':stage,'code':receipt['sha256'],'manifest':manifest_digest,'cost':key},sort_keys=True).encode()).hexdigest()
        if meta.exists():
            old=json.loads(meta.read_text())
            if old['signature']!=signature or old['sha256']!=hashlib.sha256(part.read_bytes()).hexdigest():raise ValueError('checkpoint changed')
            payload=json.load(__import__('gzip').open(part,'rt'));results[key],journals[key]=payload['result'],payload['journal'];print('reused',stage,key,flush=True)
        else:
            if part.exists():raise ValueError('orphan checkpoint preserved')
            results[key],journals[key]=simulate(markets,data,stress,begin,end)
            common.write_gzip(part,{'result':results[key],'journal':journals[key]})
            temp=meta.with_suffix('.tmp');temp.write_text(json.dumps({'signature':signature,'sha256':hashlib.sha256(part.read_bytes()).hexdigest()},indent=2)+'\n');temp.replace(meta)
            print(stage,key,results[key]['closed'],results[key]['net'],flush=True)
    year=2025 if stage=='development' else 2026;checks=eligible(results['base'],results['stress'],year)
    common.write_gzip(out/(stage+'_journal.json.gz'),journals)
    summary={'stage':stage+str(year),'selected':'legacy_vwap_1h_fixed','universe':list(ASSETS),'results':results,'checks':checks,'candidate':all(checks.values()),
             'trading_admitted':False,'sources':proofs,'manifest_sha256':manifest_digest,
             'journal_sha256':hashlib.sha256((out/(stage+'_journal.json.gz')).read_bytes()).hexdigest()}
    if selection:summary['selection_sha256']=hashlib.sha256((out/'selection.json').read_bytes()).hexdigest()
    (out/(stage+'.json')).write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (out/(stage+'_sources.json')).write_bytes(manifest.read_bytes())
    if stage=='development':(out/'selection.json').write_text(json.dumps({'candidate':all(checks.values()),'development_summary_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'policy':'legacy_vwap_1h_fixed','universe':list(ASSETS),'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'candidate':summary['candidate'],'checks':checks,'results':{k:{'closed':v['closed'],'net':v['net'],'by_asset':v['by_asset']} for k,v in results.items()}},indent=2))


if __name__=='__main__':run(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[1])
