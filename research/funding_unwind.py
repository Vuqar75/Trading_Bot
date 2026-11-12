"""One fixed post-settlement price-response experiment, entirely offline."""
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
import sys

import numpy as np

from research import extreme_reversal as core
from research import taker_absorption as common
from research.technical_patterns import rma


def signals(trade, funding, start):
    if len(trade) % 15 or not np.isfinite(trade).all():
        raise ValueError('incomplete signal prices')
    a = trade.reshape(-1, 15, 4)
    h, l, c = a[:, :, 1].max(1), a[:, :, 2].min(1), a[:, -1, 3]
    prev = np.r_[c[0], c[:-1]]
    atr = rma(np.maximum(h-l, np.maximum(abs(h-prev), abs(l-prev))), 14)
    result, seen = [], set()
    for t, rate in funding:
        if type(t) is not int or t in seen or not isinstance(rate, (int, float, np.floating)) or not np.isfinite(rate):
            raise ValueError('funding identity or rate')
        seen.add(t)
        i = (t-start)//(15*core.M)-1
        if abs(rate) < .0003 or i < 0 or i >= len(c) or not np.isfinite(atr[i]):
            continue
        # Archive settlement stamps can be boundary+1ms. Never floor the
        # entry to an earlier open: conservatively ceil the decision to1min.
        decision = ((t+core.M-1)//core.M)*core.M
        result.append({'T': decision, 'funding_event_ms': t, 'funding_rate': rate,
                       'side': -1 if rate > 0 else 1, 'atr_pct': float(atr[i]/c[i]),
                       'last_closed_price_ms': start+(i+1)*15*core.M,
                       'settled_funding_is_earned': False})
    return sorted(result, key=lambda r: r['T'])


def criteria(base, stress):
    checks = {'counts': all(v['closed'] >= 30 and min(v['directions'].values()) >= 10 for v in (base, stress)),
            'positive_base_stress': base['net'] > 0 and stress['net'] > 0,
            'stress_three_segments_positive': sum(n > 0 for n in stress['segments'].values()) >= 3,
            'stress_without_best_positive': stress['without_best'] > 0,
            'drawdown_within_2pct': max(base['drawdown'], stress['drawdown']) <= .02}
    return {k: bool(v) for k, v in checks.items()}


def account_from_saved(records, mark, stress):
    """Recover lost summary serialization only. Never generate a new fill."""
    account = {'cash': 1000., 'peak': 1000., 'drawdown': 0.}
    closed = [r['outcome'] for r in records if 'net' in r.get('outcome', {})]
    for outcome in closed:
        common.update_drawdown(account, outcome, mark, core.START, stress)
    for k in ('gross', 'fees', 'impact', 'funding', 'liquidation_fee', 'net'):
        account[k] = sum(r[k] for r in closed)
    account['closed'] = len(closed)
    account['directions'] = {str(side): sum(r['side'] == side for r in closed) for side in (-1, 1)}
    account['rejections'] = dict(Counter(r.get('rejection') or r.get('outcome', {}).get('rejection') for r in records
                                       if r.get('rejection') or r.get('outcome', {}).get('rejection')))
    account['ignored_open'] = sum('ignored' in r for r in records)
    account['exit_reasons'] = dict(Counter(r['reason'] for r in closed))
    account['segments'] = {name: sum(r['net'] for r in closed if lo <= r['exit_ms'] < hi) for name, lo, hi in core.SEGMENTS}
    account['without_best'] = account['net']-max((r['net'] for r in closed), default=0.)
    return account


def main(root, out, *, recover_saved=False):
    common.execution.r.START = core.START
    result, journals, sources, evaluations = {}, {}, {}, {}
    if recover_saved:
        journal_hash = hashlib.sha256((out/'journal.json.gz').read_bytes()).hexdigest()
        journals = json.load(gzip.open(out/'journal.json.gz', 'rt'))
    for symbol in common.ASSETS:
        trade, mark, fund, proof, conflicts = core.load_data(root, symbol)
        if conflicts or not np.isfinite(trade).all() or not np.isfinite(mark).all():
            raise ValueError('source conflict/gap')
        if not recover_saved:
            candidates = signals(trade, fund, core.START)
            cut = 15*6000
            prefix = signals(trade[:cut], [(t, r) for t, r in fund if t <= core.START+cut*core.M], core.START)
            assert prefix == [r for r in candidates if r['T'] <= core.START+cut*core.M]
        sources[symbol] = proof
        for stress in (False, True):
            key = symbol+('/stress' if stress else '/base')
            if recover_saved:
                result[key] = account_from_saved(journals[key], mark, stress)
            else:
                journals[key], result[key] = common.replay(candidates, trade, mark, fund, symbol, stress)
            print(key, result[key]['closed'], result[key]['net'], flush=True)
        check = criteria(result[symbol+'/base'], result[symbol+'/stress'])
        evaluations[symbol] = {'checks': check, 'candidate': all(check.values())}
    out.mkdir(parents=True, exist_ok=True)
    if not recover_saved:
        raw = json.dumps(journals, sort_keys=True, separators=(',', ':')).encode()
        with (out/'journal.json.gz').open('wb') as stream:
            with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as f:
                f.write(raw)
    elif hashlib.sha256((out/'journal.json.gz').read_bytes()).hexdigest() != journal_hash:
        raise ValueError('saved journal changed during recovery')
    summary = {'protocol': 'FUNDING_UNWIND_PROTOCOL.md', 'sources': sources, 'results': result,
               'evaluations': evaluations, 'trading_admitted': False, 'fits': 0,
               'prefix_causality': 'PASS_in_original_run' if recover_saved else 'PASS',
               'recovered_summary_only': recover_saved,
               'journal_sha256': hashlib.sha256((out/'journal.json.gz').read_bytes()).hexdigest()}
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(evaluations, indent=2))


if __name__ == '__main__':
    main(Path(sys.argv[1]), Path(sys.argv[2]), recover_saved='--recover-saved-summary' in sys.argv[3:])
