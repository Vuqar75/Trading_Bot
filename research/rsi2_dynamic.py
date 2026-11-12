"""Fixed source-based RSI2 entry and mean exit; sequential development only."""
import bisect
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research import extreme_reversal as core
from research import rsi15m_verification as loader
from research.rsi_crossasset import sizing
from research.technical_patterns import rma

M = core.M
ASSETS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
HOLDS = {15: 360, 60: 720, 240: 1440}


def indicators(trade, minutes):
    if minutes not in HOLDS or len(trade) % minutes or not np.isfinite(trade).all():
        raise ValueError('signal input')
    close = trade[minutes-1::minutes, 3]
    delta = np.r_[np.nan, np.diff(close)]
    up, down = rma(np.maximum(delta, 0), 2), rma(np.maximum(-delta, 0), 2)
    total = up+down
    rsi = np.divide(100*up, total, out=np.full(len(close), 50.), where=total>0)
    rsi[~np.isfinite(total)] = np.nan
    sma200 = pd.Series(close).rolling(200, min_periods=200).mean().to_numpy()
    sma5 = pd.Series(close).rolling(5, min_periods=5).mean().to_numpy()
    previous = np.r_[np.nan, rsi[:-1]]
    side = np.zeros(len(close), dtype=np.int8)
    side[(close>sma200)&(rsi<5)&(previous>=5)] = 1
    side[(close<sma200)&(rsi>95)&(previous<=95)] = -1
    return {'close': close, 'rsi': rsi, 'sma200': sma200, 'sma5': sma5, 'side': side}


def account_class():
    Base, source_sha = loader.load_account_class()
    # Only the isolated extracted class uses this proxy; no global risk
    # function, trading process or operating setting is mutated.
    Base.enter.__globals__['core'] = SimpleNamespace(size_for=sizing, funding_amount=core.funding_amount, segment=core.segment)

    class Timed(Base):
        def __init__(self, symbol, stress, minutes):
            super().__init__(symbol, stress, True)
            self.minutes = minutes

        def enter(self, t, target, raw, mark, signal):
            super().enter(t, target, raw, mark, signal)
            if self.position:
                self.position['deadline_ms'] = t+HOLDS[self.minutes]*M

        def close(self, t, raw, reason, liqrate=0.):
            super().close(t, raw, 'mean_exit' if reason=='invalid_signal' else reason, liqrate)

        def tick(self, t, bar, mark, events):
            if self.position and t >= self.position['deadline_ms']:
                if self.pending:
                    self.events.append({'time_ms': t, 'action': 'cancel_pending', 'reason': 'time_limit'})
                    self.pending = None
                self.close(t, float(bar[0]), 'time')
            super().tick(t, bar, mark, events)

    return Timed, source_sha


def summarize(account):
    trades = account.trades
    result = {key: float(sum(r[key] for r in trades)) for key in ('gross', 'fees', 'impact', 'funding', 'liquidation_fee', 'net')}
    result.update(closed=len(trades), long=sum(r['sign']==1 for r in trades), short=sum(r['sign']==-1 for r in trades),
                  cash=float(account.cash), drawdown=float(account.dd), open=account.position, pending=account.pending,
                  unresolved=account.halted, exit_reasons=dict(Counter(r['reason'] for r in trades)),
                  rejections=dict(Counter(r['reason'] for r in account.events if r['action']=='reject')),
                  without_best=float(result['net']-max((r['net'] for r in trades), default=0.)),
                  mean_hold_hours=float(np.mean([r['hold_hours'] for r in trades])) if trades else None,
                  max_hold_hours=max((r['hold_hours'] for r in trades), default=None),
                  segments={name: float(sum(r['net'] for r in trades if lo<=r['exit_ms']<hi)) for name,lo,hi in core.SEGMENTS})
    if not math.isclose(result['cash'], 1000+result['net'], abs_tol=1e-7):
        raise ValueError('cash ledger')
    if account.position or account.pending or account.halted:
        raise ValueError('unresolved end state')
    return result


def simulate(trade, mark, funding, data, symbol, minutes, stress, begin, end):
    Timed, _ = account_class()
    account = Timed(symbol, stress, minutes)
    origin = core.START
    entry_slots = ((np.flatnonzero(data['side'])+1)*minutes).tolist()
    fby = {}
    for t, rate in funding:
        fby.setdefault((t-origin)//M, []).append((t, rate))
    first, last = (begin-origin)//M, (end-origin)//M
    i, decisions = first, []
    while i < last:
        if not account.position and not account.pending:
            k = bisect.bisect_left(entry_slots, i)
            if k == len(entry_slots) or entry_slots[k] >= last:
                break
            i = entry_slots[k]
        t = origin+i*M
        if i % minutes == 0:
            j = i//minutes-1
            if j >= 0:
                side = int(data['side'][j])
                context = {'time_ms': t, 'close': float(data['close'][j]), 'rsi2': float(data['rsi'][j]),
                           'sma200': float(data['sma200'][j]), 'sma5': float(data['sma5'][j]), 'entry_side': side}
                if account.position and not account.pending:
                    current = account.current()
                    mean_exit = data['close'][j]>data['sma5'][j] if current==1 else data['close'][j]<data['sma5'][j]
                    if mean_exit:
                        context['action'] = 'mean_exit'
                        account.signal(t, 0, context)
                        decisions.append(context)
                    elif side:
                        decisions.append({**context, 'action': 'ignored_position_open'})
                elif not account.position and not account.pending and side:
                    if t+(6 if stress else 1)*M+HOLDS[minutes]*M >= end:
                        account.events.append({'time_ms': t, 'action': 'reject', 'reason': 'incomplete_end_window'})
                    else:
                        context['action'] = 'entry'
                        account.signal(t, side, context)
                        decisions.append(context)
        if account.position or account.pending:
            account.tick(t, trade[i], mark[i], fby.get(i, []))
        if account.halted:
            raise ValueError('account halted')
        i += 1
    return summarize(account), {'trades': account.trades, 'events': account.events, 'signals': account.signals, 'decisions': decisions}


def eligible(base, stress, year):
    checks = {'counts': all(r['closed']>=30 and r['long']>=10 and r['short']>=10 for r in (base,stress)),
              'positive_both': base['net']>0 and stress['net']>0,
              'both_halves_positive': all(stress['segments'][str(year)+'H'+str(k)]>0 for k in (1,2)),
              'without_best_positive': stress['without_best']>0,
              'drawdown': max(base['drawdown'],stress['drawdown'])<=.02}
    return {k: bool(v) for k,v in checks.items()}


def write_gzip(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    with path.open('wb') as f:
        with gzip.GzipFile(fileobj=f, mode='wb', filename='', mtime=0) as z:
            z.write(raw)


def development(root, out):
    results, journals, sources, checks = {}, {}, {}, {}
    end = core.stamp('2026-01-01')
    for symbol in ASSETS:
        trade, mark, fund, proof, conflicts = core.load_data(root, symbol)
        if conflicts or not np.isfinite(trade).all() or not np.isfinite(mark).all():
            raise ValueError('source gap/conflict')
        sources[symbol] = proof
        for minutes in HOLDS:
            # Development features do not even receive the2026 price tail.
            limit = (end-core.START)//M
            data = indicators(trade[:limit], minutes)
            prefix = indicators(trade[:minutes*400], minutes)
            for key in data:
                np.testing.assert_allclose(data[key][:400], prefix[key], equal_nan=True)
            key = symbol+'/'+str(minutes)
            for stress in (False, True):
                case = key+('/stress' if stress else '/base')
                results[case], journals[case] = simulate(trade, mark, fund, data, symbol, minutes, stress, core.BEGIN, end)
                print(case, results[case]['closed'], results[case]['net'], flush=True)
            checks[key] = eligible(results[key+'/base'], results[key+'/stress'], 2025)
    allowed = [key for key,v in checks.items() if all(v.values())]
    allowed.sort(key=lambda k: (-results[k+'/stress']['net'], -results[k+'/stress']['closed'], k))
    chosen = allowed[0] if allowed else None
    out.mkdir(parents=True, exist_ok=True)
    write_gzip(out/'development_journal.json.gz', journals)
    summary = {'stage': 'development2025', 'results': results, 'checks': checks, 'selected': chosen, 'sources': sources,
               'validation_performed': False, 'trading_admitted': False,
               'journal_sha256': hashlib.sha256((out/'development_journal.json.gz').read_bytes()).hexdigest()}
    (out/'development.json').write_text(json.dumps(summary, indent=2, allow_nan=False)+'\n')
    selection = {'selected': chosen, 'development_summary_sha256': hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),
                 'validation_performed': False, 'trading_admitted': False}
    (out/'selection.json').write_text(json.dumps(selection, indent=2)+'\n')
    print(json.dumps({'selected': chosen, 'checks': checks}, indent=2))


def validation(root, out):
    selection = json.loads((out/'selection.json').read_text())
    if hashlib.sha256((out/'development.json').read_bytes()).hexdigest() != selection['development_summary_sha256']:
        raise ValueError('development changed')
    if selection['selected'] is None:
        raise ValueError('no development candidate; no validation scan')
    symbol, minutes = selection['selected'].split('/'); minutes = int(minutes)
    trade, mark, fund, proof, conflicts = core.load_data(root, symbol)
    if conflicts:
        raise ValueError('source conflict')
    data = indicators(trade, minutes)
    results, journals = {}, {}
    for stress in (False, True):
        case = 'stress' if stress else 'base'
        results[case], journals[case] = simulate(trade, mark, fund, data, symbol, minutes, stress, core.stamp('2026-01-01'), core.END)
        print('validation', selection['selected'], case, results[case]['closed'], results[case]['net'], flush=True)
    checks = eligible(results['base'], results['stress'], 2026)
    write_gzip(out/'validation_journal.json.gz', journals)
    summary = {'stage': 'validation2026', 'selected': selection['selected'], 'results': results, 'checks': checks,
               'candidate': all(checks.values()), 'trading_admitted': False, 'sources': proof,
               'selection_sha256': hashlib.sha256((out/'selection.json').read_bytes()).hexdigest(),
               'journal_sha256': hashlib.sha256((out/'validation_journal.json.gz').read_bytes()).hexdigest()}
    (out/'validation.json').write_text(json.dumps(summary, indent=2, allow_nan=False)+'\n')
    print(json.dumps({'candidate': summary['candidate'], 'checks': checks}, indent=2))


if __name__ == '__main__':
    {'development': development, 'validation': validation}[sys.argv[1]](Path(sys.argv[2]), Path(sys.argv[3]))
