"""One offline trade-volume/price-response hypothesis. No network or fitting."""
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from research import extreme_reversal as core
from research import pattern_ai_experiment as execution
from research.technical_patterns import rma
from research.usdm_intraday_data import zipped

M = core.M
BAR = 15*M
ASSETS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')


def volumes(root, proof, n, start):
    volume, buy = np.full(n, np.nan), np.full(n, np.nan)
    count = 0
    for item in proof:
        if not item['file'].startswith('minute/klines/'):
            continue
        path = root/item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('volume input revision')
        count += 1
        for row in zipped(path):
            if not row[0].isdigit():
                if row[0] != 'open_time' or row[5] != 'volume' or row[9] != 'taker_buy_volume':
                    raise ValueError('volume header')
                continue
            t = int(row[0]); i = (t-start)//M
            if not 0 <= i < n:
                continue
            if t % M or np.isfinite(volume[i]):
                raise ValueError('volume timestamp or duplicate')
            v, b = float(row[5]), float(row[9])
            if not math.isfinite(v) or not math.isfinite(b) or not 0 <= b <= v:
                raise ValueError('volume bounds')
            volume[i], buy[i] = v, b
    if not count or not np.isfinite(volume).all() or not np.isfinite(buy).all():
        raise ValueError('missing volume input')
    return volume, buy


def signals(trade, volume, buy, start):
    if len(trade) % 15 or len(volume) != len(trade) or len(buy) != len(trade):
        raise ValueError('unaligned volumes')
    if not np.isfinite(trade).all() or not np.isfinite(volume).all() or not np.isfinite(buy).all():
        raise ValueError('missing signal inputs')
    if np.any(buy < 0) or np.any(volume < buy):
        raise ValueError('volume bounds')
    a = trade.reshape(-1, 15, 4)
    o, h, l, c = a[:, 0, 0], a[:, :, 1].max(1), a[:, :, 2].min(1), a[:, -1, 3]
    v, b = volume.reshape(-1, 15).sum(1), buy.reshape(-1, 15).sum(1)
    previous = np.r_[c[0], c[:-1]]
    tr = np.maximum(h-l, np.maximum(abs(h-previous), abs(l-previous)))
    atr = np.r_[np.nan, rma(tr, 14)[:-1]]
    mean = pd.Series(v).shift(1).rolling(96, min_periods=96).mean().to_numpy()
    imbalance = np.divide(2*b-v, v, out=np.zeros_like(v), where=v>0)
    setups, output = [], []
    for i in range(96, len(c)-1):
        if not (np.isfinite(mean[i]) and np.isfinite(atr[i]) and atr[i]>0 and
                v[i]>0 and v[i]>=1.5*mean[i] and abs(imbalance[i])>=.20 and abs(c[i]-o[i])<=.25*atr[i]):
            continue
        side = -1 if imbalance[i] > 0 else 1
        confirmed = bool(c[i+1] < l[i] if side == -1 else c[i+1] > h[i])
        row = {'setup_ms': start+(i+1)*BAR, 'T': start+(i+2)*BAR, 'side': side,
               'volume': float(v[i]), 'taker_buy_volume': float(b[i]), 'imbalance': float(imbalance[i]),
               'previous_volume_mean': float(mean[i]), 'prior_atr': float(atr[i]),
               'atr_pct': float(atr[i]/c[i]), 'setup_open': float(o[i]), 'setup_close': float(c[i]),
               'setup_high': float(h[i]), 'setup_low': float(l[i]), 'confirmation_close': float(c[i+1]),
               'confirmed': confirmed}
        setups.append(row)
        if confirmed:
            output.append(row)
    return output, setups


def update_drawdown(account, outcome, mark, start, stress):
    fee, impact = .0005, .001 if stress else .0005
    q, fill, side = outcome['qty'], outcome['entry'], outcome['side']
    events = outcome['funding_events']; funding = 0.
    cash = account['cash'] - outcome['entry_fee']
    for i in range((outcome['entry_ms']-start)//M, (outcome['exit_ms']-start)//M):
        t = start+i*M
        funding += sum(e['applied'] for e in events if t <= e['event_ms'] < t+M)
        high, low = mark[i, 1], mark[i, 2]
        favorable, adverse = (high, low) if side == 1 else (low, high)
        for price in (favorable, adverse):
            equity = cash + funding + side*q*(price-fill) - q*price*(fee+impact)
            account['peak'] = max(account['peak'], equity)
            account['drawdown'] = max(account['drawdown'], (account['peak']-equity)/account['peak'])
    account['cash'] += outcome['net']
    account['peak'] = max(account['peak'], account['cash'])
    account['drawdown'] = max(account['drawdown'], (account['peak']-account['cash'])/account['peak'])


def replay(candidates, trade, mark, funding, symbol, stress):
    records, busy = [], core.BEGIN
    account = {'cash': 1000., 'peak': 1000., 'drawdown': 0.}
    for signal in candidates:
        if not core.BEGIN <= signal['T'] < core.END:
            continue
        record = {'signal': signal, 'symbol': symbol}
        if signal['T'] < busy:
            record['ignored'] = 'position_open'
        else:
            result = execution.outcome(signal['T'], signal['side'], signal['atr_pct'], symbol, trade, mark, funding, stress)
            record['outcome'] = result
            if 'net' in result:
                mark_entry = float(mark[(result['entry_ms']-core.START)//M, 0])
                notional = result['qty']*max(result['entry'], mark_entry)
                if notional > 100+1e-8:
                    record = {'signal': signal, 'symbol': symbol, 'rejection': 'notional_cap_100',
                              'proposed_notional': notional}
                elif result['qty']*result['entry']+result['entry_fee'] > account['cash']:
                    record = {'signal': signal, 'symbol': symbol, 'rejection': 'insufficient_cash'}
                else:
                    update_drawdown(account, result, mark, core.START, stress)
                    busy = result['exit_ms']
        records.append(record)
    closed = [r['outcome'] for r in records if 'net' in r.get('outcome', {})]
    for k in ('gross', 'fees', 'impact', 'funding', 'liquidation_fee', 'net'):
        account[k] = sum(r[k] for r in closed)
    assert math.isclose(account['cash'], 1000+account['net'], abs_tol=1e-8)
    account['closed'] = len(closed)
    account['directions'] = {str(side): sum(r['side']==side for r in closed) for side in (-1, 1)}
    account['rejections'] = dict(Counter(r.get('rejection') or r.get('outcome', {}).get('rejection') for r in records
                                       if r.get('rejection') or r.get('outcome', {}).get('rejection')))
    account['ignored_open'] = sum('ignored' in r for r in records)
    account['exit_reasons'] = dict(Counter(r['reason'] for r in closed))
    account['segments'] = {name: sum(r['net'] for r in closed if lo <= r['exit_ms'] < hi) for name, lo, hi in core.SEGMENTS}
    account['without_best'] = account['net']-max((r['net'] for r in closed), default=0.)
    return records, account


def main(root, out):
    # outcome uses only START from this module; align it explicitly to the
    # pinned source origin, without mutating any VM or shared process.
    execution.r.START = core.START
    results, journals, source_receipts, setup_journal = {}, {}, {}, {}
    for symbol in ASSETS:
        trade, mark, fund, proof, conflicts = core.load_data(root, symbol)
        if conflicts or not np.isfinite(trade).all() or not np.isfinite(mark).all():
            raise ValueError('price gap/conflict')
        volume, buy = volumes(root, proof, len(trade), core.START)
        candidates, setups = signals(trade, volume, buy, core.START)
        cut = 15*6000
        short, _ = signals(trade[:cut], volume[:cut], buy[:cut], core.START)
        assert short == [s for s in candidates if s['T'] <= core.START+cut*M]
        source_receipts[symbol] = proof
        setup_journal[symbol] = [s for s in setups if core.BEGIN <= s['T'] < core.END]
        for stress in (False, True):
            key = symbol+('/stress' if stress else '/base')
            journals[key], results[key] = replay(candidates, trade, mark, fund, symbol, stress)
            print(key, results[key]['closed'], results[key]['net'], flush=True)
    stress_rows = [r['outcome'] for k, rows in journals.items() if k.endswith('/stress') for r in rows if 'net' in r.get('outcome', {})]
    checks = {'counts_and_sides': all(v['closed'] >= 30 and min(v['directions'].values()) >= 10 for v in results.values()),
              'all_asset_scenarios_positive': all(v['net'] > 0 for v in results.values()),
              'drawdown_within_2pct': all(v['drawdown'] <= .02 for v in results.values()),
              'stress_three_segments_positive': sum(sum(v['segments'][name] for k, v in results.items() if k.endswith('/stress')) > 0 for name, _, _ in core.SEGMENTS) >= 3,
              'stress_without_best_positive': sum(r['net'] for r in stress_rows)-max((r['net'] for r in stress_rows), default=0.) > 0}
    out.mkdir(parents=True, exist_ok=True)
    for name, value in [('journal', journals), ('setups', setup_journal)]:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        with (out/(name+'.json.gz')).open('wb') as f:
            with gzip.GzipFile(fileobj=f, mode='wb', filename='', mtime=0) as z:
                z.write(raw)
    summary = {'protocol': 'TAKER_ABSORPTION_PROTOCOL.md', 'sources': source_receipts, 'results': results,
               'setup_counts': {s: len(r) for s, r in setup_journal.items()},
               'confirmed_counts': {s: sum(x['confirmed'] for x in r) for s, r in setup_journal.items()},
               'checks': checks, 'screen_pass': all(checks.values()), 'trading_admitted': False,
               'prefix_causality': 'PASS', 'fits': 0,
               'artifacts_sha256': {n: hashlib.sha256((out/n).read_bytes()).hexdigest() for n in ('journal.json.gz', 'setups.json.gz')}}
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps({'setup_counts': summary['setup_counts'], 'confirmed_counts': summary['confirmed_counts'], 'checks': checks}, indent=2))


if __name__ == '__main__':
    main(Path(sys.argv[1]), Path(sys.argv[2]))
