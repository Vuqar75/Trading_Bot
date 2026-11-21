"""Offline funding income feasibility, frozen in EDGE_RESEARCH_PROGRAM.md."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from decimal import Decimal
import argparse
import gzip
import json
from pathlib import Path

from research import extreme_reversal as core
from research.flow_weekend import metrics
from research.mark_dislocation import simulate, SPLIT

SLOT = 480 * core.M
HOLD = 1440


def decisions(funding, begin, end):
    times = [t for t, _ in funding]
    signals = []
    for slot in range(begin // SLOT, end // SLOT):
        t = slot * SLOT + 15 * core.M
        i = bisect_right(times, t)
        history = funding[max(i - 3, 0):i]
        if [ts // SLOT for ts, _ in history] != [slot-2, slot-1, slot]:
            continue
        rates = [Decimal(str(rate)) for _, rate in history]
        total = sum(rates)
        if (min(rates) > 0 or max(rates) < 0) and abs(total) >= Decimal('.003'):
            signals.append(dict(signal_ms=t, direction='SHORT' if total > 0 else 'LONG',
                                known_settlements=history, trailing_bps=float(total*10000)))
    return signals


def ceilings(trade, mark, funding, delay):
    times = [t for t, _ in funding]
    windows = []
    for slot in range(core.BEGIN // SLOT, core.END // SLOT):
        signal = slot * SLOT + 15 * core.M
        entry = signal + delay * core.M
        exit_time = entry + HOLD * core.M
        if exit_time >= core.END:
            continue
        ref = trade[core.ix(entry), 0]
        future = funding[bisect_left(times, entry):bisect_left(times, exit_time)]
        if len(future) != 3 or not ref > 0:
            raise ValueError('unconfirmed ceiling window')
        amounts = [float(mark[core.ix(ts-ts%core.M), 0]/ref*rate*10000) for ts, rate in future]
        if not all(abs(x) < float('inf') for x in amounts):
            raise ValueError('unconfirmed funding mark')
        windows.append(dict(signal_ms=signal, ceiling_bps=sum(map(abs, amounts))))
    return windows


def run(root, out):
    inputs = json.loads(Path(__file__).with_name('btc_eth_delay_results').joinpath('inputs.json').read_text())
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for symbol, pinned in inputs.items():
        trade, mark, funding, files, overlaps = core.load_data(root, symbol)
        if files != pinned['files'] or overlaps != pinned['archive_rest_differences']:
            raise ValueError('source revision')
        fby = defaultdict(list)
        for t, rate in funding:
            fby[core.ix(t-t%core.M)].append((t, rate))
        signals = decisions(funding, core.BEGIN, core.END)
        for stress in (False, True):
            name = symbol + ('-stress' if stress else '-base')
            records, account = simulate(signals, trade, mark, fby, symbol, stress, hold_minutes=HOLD)
            oracle = ceilings(trade, mark, funding, 6 if stress else 1)
            overall = metrics(records, account)
            gates = {'resolved':not overall['unresolved'], 'drawdown':account['max_dd']<=.02,
                     'funding_covers_costs':overall['closed']>0 and overall['funding'] >= overall['impact']+overall['fees']}
            if gates['resolved'] and abs(account['cash'] - 1000 - overall['net']) > 1e-8:
                raise ValueError('ledger mismatch')
            periods = {}
            for label, start, end in (('2025',core.BEGIN,SPLIT),('2026',SPLIT,core.END)):
                m = metrics([r for r in records if start <= r['signal_ms'] < end])
                amounts = [r['ceiling_bps'] for r in oracle if start <= r['signal_ms'] < end]
                m['ceiling'] = dict(windows=len(amounts), max_bps=max(amounts),
                                    reaching_bps={k:sum(v>=k for v in amounts) for k in (10,20,30)})
                gates[label+'_count'] = m['closed'] >= 30
                gates[label+'_sides'] = min(v['count'] for v in m['sides'].values())>=10
                gates[label+'_net'] = m['net']>0
                if stress:
                    gates[label+'_ex_best_day'] = m['net_without_best_day']>0
                periods[label] = m
            results[name] = dict(all=overall, periods=periods, gates=gates)
            data = json.dumps(dict(records=records, hindsight_ceiling=oracle),allow_nan=False).encode()
            (out/(name+'.json.gz')).write_bytes(gzip.compress(data,mtime=0))
    (out/'summary.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n',encoding='utf-8')


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive',type=Path)
    p.add_argument('output',type=Path)
    args=p.parse_args()
    run(args.archive,args.output)
