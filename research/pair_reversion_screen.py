"""A single fixed offline relative-value return screen, not paper executions."""
import gzip
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research import extreme_reversal as core

M = core.M


def signals(btc, eth, start):
    if len(btc) != len(eth) or len(btc) % 15:
        raise ValueError('aligned complete minute arrays required')
    # Require each whole 15m window, not just its last price.
    valid = np.isfinite(btc).all(axis=1) & np.isfinite(eth).all(axis=1)
    valid = valid.reshape(-1, 15).all(axis=1)
    spread = np.log(eth[14::15, 3] / btc[14::15, 3])
    spread[~valid] = np.nan
    history = pd.Series(spread).shift(1).rolling(96, min_periods=96)
    mean = history.mean().to_numpy()
    std = history.std(ddof=1).to_numpy()
    with np.errstate(divide='ignore', invalid='ignore'):
        z = (spread - mean) / std
    result = []
    for i in range(97, len(spread)):
        if not np.isfinite(z[i-1:i+1]).all() or not std[i] > 0:
            continue
        if abs(z[i-1]) >= 2 or abs(z[i]) < 2 or abs(spread[i] - mean[i]) / 2 < .0035:
            continue
        result.append({'signal_ms': start + (i+1)*15*M, 'eth_side': -1 if z[i] > 0 else 1,
                       'z': float(z[i]), 'previous_z': float(z[i-1]), 'spread': float(spread[i]),
                       'prior_mean': float(mean[i]), 'prior_std': float(std[i]),
                       'hypothesized_move_gross_bps': float(abs(spread[i]-mean[i])*5000)})
    return result


def evaluate(signal, markets, stress, start):
    entry = signal['signal_ms'] + (6 if stress else 1)*M
    exit_ = entry + 240*M
    ei, xi = (entry-start)//M, (exit_-start)//M
    impact = .001 if stress else .0005
    fee = .0005
    legs = []
    adverse = np.zeros(xi-ei)
    for symbol, side in (('ETHUSDT', signal['eth_side']), ('BTCUSDT', -signal['eth_side'])):
        trade, mark, funding = markets[symbol]
        if not 0 <= ei < xi < len(trade):
            return None
        if not np.isfinite(trade[ei:xi+1]).all() or not np.isfinite(mark[ei:xi+1]).all():
            raise ValueError('incomplete price/mark path')
        raw_entry, raw_exit = float(trade[ei, 0]), float(trade[xi, 0])
        qty = 1 / raw_entry
        fill_entry = raw_entry*(1+side*impact)
        fill_exit = raw_exit*(1-side*impact)
        gross = qty*side*(raw_exit-raw_entry)
        fees = qty*(fill_entry+fill_exit)*fee
        friction = qty*(abs(fill_entry-raw_entry)+abs(fill_exit-raw_exit))
        events = [{'time_ms': int(t), 'rate': rate, 'mark': float(mark[(t-start)//M, 0]),
                   'amount': -side*qty*float(mark[(t-start)//M, 0])*rate}
                  for t, rate in funding if entry < t < exit_]
        fund = sum(e['amount'] for e in events)
        # Sum each leg's adverse mark extreme. Extremes need not coincide:
        # this is a conservative bound, not observed executable portfolio PnL.
        adverse += qty*side*(mark[ei:xi, 2 if side == 1 else 1]-raw_entry)
        legs.append({'symbol': symbol, 'side': side, 'qty': qty, 'entry_reference': raw_entry,
                     'exit_reference': raw_exit, 'entry_fill_model': fill_entry, 'exit_fill_model': fill_exit,
                     'gross': gross, 'fees': fees, 'impact': friction, 'funding': fund,
                     'funding_events': events, 'net': gross-fees-friction+fund})
    totals = {k: sum(x[k] for x in legs) for k in ('gross', 'fees', 'impact', 'funding', 'net')}
    return {**signal, 'entry_ms': entry, 'exit_ms': exit_, 'legs': legs, **totals,
            'net_bps_gross_notional': totals['net']*5000,
            'adverse_mark_bound_bps': float(min(0, adverse.min())*5000)}


def summarize(rows):
    n = len(rows)
    result = {'pairs': n, 'mean_net_bps': float(np.mean([r['net_bps_gross_notional'] for r in rows])) if n else None,
              'direction_counts': {str(s): sum(r['eth_side'] == s for r in rows) for s in (-1, 1)},
              'sums_per_unit_pair': {k: sum(r[k] for r in rows) for k in ('gross', 'fees', 'impact', 'funding', 'net')},
              'net_without_best_pair': sum(r['net'] for r in rows)-max((r['net'] for r in rows), default=0),
              'worst_adverse_mark_bound_bps': min((r['adverse_mark_bound_bps'] for r in rows), default=None),
              'segments': {}}
    for name, lo, hi in core.SEGMENTS:
        subset = [r for r in rows if lo <= r['exit_ms'] < hi]
        result['segments'][name] = {'pairs': len(subset), 'sum_net': sum(r['net'] for r in subset)}
    return result


def main(root, out):
    markets, sources = {}, {}
    for symbol in ('BTCUSDT', 'ETHUSDT'):
        trade, mark, funding, proof, conflicts = core.load_data(root, symbol)
        if conflicts or not np.isfinite(trade).all() or not np.isfinite(mark).all():
            raise ValueError('source gaps/conflicts')
        markets[symbol] = trade, mark, funding
        sources[symbol] = proof
        print('verified', symbol, len(proof), 'inputs', flush=True)
    candidates = signals(markets['BTCUSDT'][0], markets['ETHUSDT'][0], core.START)
    # Changing the future cannot change earlier signals.
    cut = 96*15*50
    prefix = signals(markets['BTCUSDT'][0][:cut], markets['ETHUSDT'][0][:cut], core.START)
    assert prefix == [s for s in candidates if s['signal_ms'] <= core.START+cut*M]
    cases, result = {}, {}
    for stress in (False, True):
        key = 'stress' if stress else 'base'
        rows, available, excluded_end = [], core.BEGIN, 0
        for sig in candidates:
            if not core.BEGIN <= sig['signal_ms'] < core.END or sig['signal_ms'] < available:
                continue
            row = evaluate(sig, markets, stress, core.START)
            if row is None:
                excluded_end += 1
                continue
            rows.append(row)
            available = row['exit_ms']
        cases[key] = rows
        result[key] = {**summarize(rows), 'excluded_incomplete_end_window': excluded_end}
    checks = {'counts_and_sides': all(v['pairs'] >= 60 and min(v['direction_counts'].values()) >= 10 for v in result.values()),
              'both_mean_net_positive': all(v['mean_net_bps'] is not None and v['mean_net_bps'] > 0 for v in result.values()),
              'stress_three_segments_positive': sum(v['sum_net'] > 0 for v in result['stress']['segments'].values()) >= 3,
              'stress_without_best_positive': result['stress']['net_without_best_pair'] > 0}
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cases, sort_keys=True, separators=(',', ':')).encode()
    with (out/'cases.json.gz').open('wb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as z:
            z.write(payload)
    summary = {'protocol': 'PAIR_REVERSION_PROTOCOL.md', 'sources': sources,
               'source_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'case_uncompressed_sha256': hashlib.sha256(payload).hexdigest(), 'results': result,
               'checks': checks, 'screen_pass': all(checks.values()), 'trading_admitted': False,
               'actual_executions': False, 'fitted_models': 0, 'prefix_causality_check': 'PASS'}
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps({'results': result, 'checks': checks, 'screen_pass': all(checks.values())}, indent=2))


if __name__ == '__main__':
    main(Path(sys.argv[1]), Path(sys.argv[2]))
