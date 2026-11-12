"""Causal minute recovery in an hourly oscillator, with completed daily trend."""
import bisect
import datetime as dt
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research import legacy_universe_data as source
from research import legacy_vwap as portfolio
from research import rsi2_dynamic as common
from research.technical_patterns import rma

core = common.core
M = common.M
ASSETS = source.ASSETS


def features(trade, start, daily_window=200):
    """daily_window is a fixture seam; the production caller always uses 200."""
    if start % (1440*M) or len(trade) < 1440*daily_window:
        raise ValueError('UTC alignment or daily warmup')
    if trade.ndim != 2 or trade.shape[1] != 4 or not np.isfinite(trade).all():
        raise ValueError('price input')
    nh = len(trade)//60
    hours = trade[:nh*60].reshape(nh, 60, 4)
    hc = hours[:, -1, 3]
    delta = np.r_[np.nan, np.diff(hc)]
    up, down = rma(np.maximum(delta, 0), 2), rma(np.maximum(-delta, 0), 2)
    prior = np.r_[hc[0], hc[:-1]]
    hh, hl = hours[:, :, 1].max(1), hours[:, :, 2].min(1)
    atr = rma(np.maximum(hh-hl, np.maximum(abs(hh-prior), abs(hl-prior))), 14)
    mean4 = pd.Series(hc).rolling(4).mean().to_numpy()
    dc = trade[1439::1440, 3]
    daily = pd.Series(dc).rolling(daily_window).mean().to_numpy()
    endminute = np.arange(1, len(trade)+1)
    j, k = endminute//60-1, endminute//1440-1
    c = trade[:, 3]
    safej, safek = np.maximum(j, 0), np.maximum(k, 0)
    diff = c-hc[safej]
    u = (up[safej]+np.maximum(diff, 0))/2
    d = (down[safej]+np.maximum(-diff, 0))/2
    total = u+d
    partial = np.divide(100*u, total, out=np.full(len(c), 50.), where=total>0)
    partial[~np.isfinite(total)] = np.nan
    macro, line, av = daily[safek], mean4[safej], atr[safej]
    valid = (j>=0)&(k>=daily_window-1)&np.isfinite(partial)&np.isfinite(macro)&np.isfinite(av)
    prev = np.r_[np.nan, partial[:-1]]
    pc = np.r_[np.nan, c[:-1]]
    pmacro = np.r_[np.nan, macro[:-1]]
    samehour = endminute//60 == (endminute-1)//60
    side = np.zeros(len(c), dtype=np.int8)
    side[valid&samehour&(c>macro)&(pc>pmacro)&(prev<5)&(partial>=5)] = 1
    side[valid&samehour&(c<macro)&(pc<pmacro)&(prev>95)&(partial<=95)] = -1
    room = side*(line-c)/c
    okay = (side!=0)&(room>=.006)&(av/c>=.006)
    events, seen = {}, set()
    for i in np.flatnonzero(okay):
        hour = int(endminute[i]//60)
        if hour in seen:
            continue
        seen.add(hour)
        t = int(start+endminute[i]*M)
        events[int(endminute[i])] = {
            'time_ms': t, 'entry_side': int(side[i]), 'close': float(c[i]),
            'partial_rsi_previous': float(prev[i]), 'partial_rsi': float(partial[i]),
            'previous_close': float(pc[i]), 'daily_sma200': float(macro[i]),
            'daily_source_end_ms': int(start+(k[i]+1)*1440*M),
            'hour_source_end_ms': int(start+(j[i]+1)*60*M),
            'atr': float(av[i]), 'entry_mean': float(line[i]), 'room': float(room[i]),
            'score': float(side[i]*(line[i]-c[i])/av[i]),
            'expiry_ms': int(start+(hour+1)*60*M),
        }
    return {'events': events, 'mean4': mean4}


def account_class():
    Base, sha = common.account_class()
    financial = next(cls for cls in Base.__mro__ if cls.__name__ == 'Account')
    risk_cap = [100.]

    def risk_sizing(fill, mark, symbol, impact):
        if symbol not in ASSETS:
            raise ValueError('asset outside fixed cohort')
        price = Decimal(str(max(fill, mark)))
        step = Decimal(10 if price<=10 else 1)
        qty = float((Decimal(str(risk_cap[0]))/price/step).to_integral_value(rounding=ROUND_DOWN)*step)
        if qty<=0 or qty*mark<50-1e-8:
            return None, 'risk_granularity_or_minimum_proxy'
        return qty, None

    financial.enter.__globals__['core'] = SimpleNamespace(
        size_for=risk_sizing, funding_amount=core.funding_amount, segment=core.segment)

    class Recovery(Base):
        def __init__(self, symbol, stress, minutes=60):
            super().__init__(symbol, stress, minutes)
            self.contexts = {}

        def enter(self, t, target, raw, mark, signal_ms):
            context = self.contexts[signal_ms]
            fill = raw*(1+target*self.impact)
            if not (t<context['expiry_ms'] and target*(fill-context['daily_sma200'])>0
                    and target*(context['entry_mean']-fill)/fill>=.006
                    and context['atr']/fill>=.006):
                self.events.append({'time_ms': t, 'action': 'reject', 'reason': 'expired_or_gap_geometry', 'signal_ms': signal_ms})
                return
            distance = float(np.clip(2*context['atr']/fill, .005, .05))
            risk_unit = distance+2*(self.fee+self.impact)+.0021
            risk_cap[0] = min(100., 5/risk_unit)
            super().enter(t, target, raw, mark, signal_ms)
            if self.position:
                p = self.position
                p.update(symbol=self.symbol, stop_rule='volatility_two_atr', stop_pct=distance,
                         stop_atr=context['atr'], stop=fill*(1-target*distance),
                         estimated_risk=p['qty']*fill*risk_unit)
                if p['estimated_risk']>5+1e-8:
                    raise ValueError('ATR sizing exceeded USD risk')
    return Recovery, sha


def choose(data, i):
    choices = [{'symbol': s, **data[s]['events'][i]} for s in ASSETS if i in data[s]['events']]
    return sorted(choices, key=lambda row: (-row['score'], row['symbol']))


def simulate(markets, data, stress, begin, end):
    Account, _ = account_class()
    a = Account(ASSETS[0], stress)
    slots = sorted(set(i for s in ASSETS for i in data[s]['events']))
    fby = {}
    for s in ASSETS:
        fby[s] = {}
        for t, rate in markets[s][3]:
            fby[s].setdefault((t-core.START)//M, []).append((t, rate))
    i, last = (begin-core.START)//M, (end-core.START)//M
    decisions = []
    while i<last:
        if not a.position and not a.pending:
            k = bisect.bisect_left(slots, i)
            if k==len(slots) or slots[k]>=last:
                break
            i = slots[k]
        t = core.START+i*M
        if a.position and a.position['symbol']!=a.symbol:
            raise ValueError('active asset changed')
        if a.position and not a.pending:
            j = i//60-1
            c = float(markets[a.symbol][0][i-1, 3])
            line = float(data[a.symbol]['mean4'][j])
            side = a.current()
            if side*(c-line)>0:
                context = {'time_ms': t, 'symbol': a.symbol, 'close': c, 'exit_line': line,
                           'hour_source_end_ms': core.START+(j+1)*60*M,
                           'action': 'mean_exit', 'entry_side': 0}
                a.signal(t, 0, context)
                decisions.append(context)
        if not a.position and not a.pending:
            candidates = choose(data, i)
            if candidates:
                context = {**candidates[0], 'action': 'entry', 'candidates': candidates}
                if t+(6 if stress else 1)*M+720*M>=end:
                    a.events.append({'time_ms': t, 'action': 'reject', 'reason': 'incomplete_end_window'})
                else:
                    a.symbol = context['symbol']
                    a.contexts[t] = context
                    a.signal(t, context['entry_side'], context)
                    decisions.append(context)
        if a.position or a.pending:
            trade, mark, _, _ = markets[a.symbol]
            a.tick(t, trade[i], mark[i], fby[a.symbol].get(i, []))
        if a.halted:
            raise ValueError('portfolio unresolved')
        i += 1
    result = common.summarize(a)
    result['by_asset'] = {s: {'closed': sum(p['symbol']==s for p in a.trades),
        'net': float(sum(p['net'] for p in a.trades if p['symbol']==s))} for s in ASSETS}
    result['without_best_asset'] = result['net']-max(v['net'] for v in result['by_asset'].values())
    result['active_months'] = len(set(dt.datetime.fromtimestamp(p['entry_ms']/1000,dt.timezone.utc).strftime('%Y-%m') for p in a.trades))
    result['max_concurrent_positions'] = 1 if a.trades else 0
    return result, {'trades': a.trades, 'events': a.events, 'signals': a.signals, 'decisions': decisions}


def run(root,out,stage):
    if stage not in ('development','validation'):raise ValueError('unknown stage')
    project=Path(__file__).resolve().parents[1];receipt=json.loads((project/'DAILY_RECOVERY_EXECUTION_INPUTS.json').read_text())
    for name,digest in receipt['sha256'].items():
        if hashlib.sha256((project/name).read_bytes()).hexdigest()!=digest:raise ValueError('registered code changed: '+name)
    start=core.stamp('2024-06-01') if stage=='development' else core.stamp('2025-06-01')
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
        data[symbol]=features(trade,start)
        cut=1440*205+317
        prefix=features(trade[:cut],start)
        expected={i:r for i,r in data[symbol]['events'].items() if i<=cut}
        if expected!=prefix['events']:raise ValueError('feature prefix lookahead')
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
    year=2025 if stage=='development' else 2026;checks=portfolio.eligible(results['base'],results['stress'],year)
    common.write_gzip(out/(stage+'_journal.json.gz'),journals)
    summary={'stage':stage+str(year),'selected':'daily200_hourly_rsi_minute_recovery','universe':list(ASSETS),'results':results,'checks':checks,'candidate':all(checks.values()),
             'trading_admitted':False,'sources':proofs,'manifest_sha256':manifest_digest,
             'journal_sha256':hashlib.sha256((out/(stage+'_journal.json.gz')).read_bytes()).hexdigest()}
    if selection:summary['selection_sha256']=hashlib.sha256((out/'selection.json').read_bytes()).hexdigest()
    (out/(stage+'.json')).write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (out/(stage+'_sources.json')).write_bytes(manifest.read_bytes())
    if stage=='development':(out/'selection.json').write_text(json.dumps({'candidate':all(checks.values()),'development_summary_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'policy':'daily200_hourly_rsi_minute_recovery','universe':list(ASSETS),'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'candidate':summary['candidate'],'checks':checks,'results':{k:{'closed':v['closed'],'net':v['net'],'by_asset':v['by_asset']} for k,v in results.items()}},indent=2))


if __name__=='__main__':run(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[1])
