"""Independently bind cached executions to actual settled funding records."""
import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

from research.usdm_intraday_data import zipped

ROOT = Path(__file__).resolve().parents[1]
M = 60000


def close(a, b):
    if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError(f'financial mismatch {a} vs {b}')


def stamp(s):
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)


def main(data_root):
    receipt = json.loads((ROOT/'FUNDING_UNWIND_INPUTS.json').read_text())
    for name, expected in receipt['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected:
            raise ValueError('registered source changed: '+name)
    out = ROOT/'research/funding_unwind_results'
    summary = json.loads((out/'summary.json').read_text())
    if hashlib.sha256((out/'journal.json.gz').read_bytes()).hexdigest() != summary['journal_sha256']:
        raise ValueError('journal checksum')
    if summary['journal_sha256'] != receipt['preserved_completed_journal_sha256']:
        raise ValueError('completed pre-recovery journal changed')
    journal = json.load(gzip.open(out/'journal.json.gz', 'rt'))
    original = json.loads((ROOT/'research/pattern_ai_results/sources.json').read_text())
    events = {}
    for symbol, sources in summary['sources'].items():
        if {x['file']: x['sha256'] for x in sources} != {x['file']: x['sha256'] for x in original[symbol]}:
            raise ValueError('market-source receipt mismatch')
        raw_events = {}
        for item in sources:
            if not item['file'].startswith('fundingRate/'):
                continue
            path = data_root/item['file']
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('actual funding archive changed')
            for row in zipped(path):
                if not row[0].isdigit():
                    continue
                t, rate = int(row[0]), float(row[2])
                if t in raw_events:
                    raise ValueError('duplicate actual funding')
                raw_events[t] = rate
        events[symbol] = raw_events
    verified = {}
    segment_dates = [('2025H1', '2025-01-01', '2025-07-01'), ('2025H2', '2025-07-01', '2026-01-01'),
                     ('2026H1', '2026-01-01', '2026-07-01'), ('2026H2', '2026-07-01', '2026-11-01')]
    for key, rows in journal.items():
        symbol, scenario = key.split('/')
        expected = {t: r for t, r in events[symbol].items() if stamp('2025-01-01') <= ((t+M-1)//M)*M < stamp('2026-11-01') and abs(r) >= .0003}
        if [r['signal']['funding_event_ms'] for r in rows] != sorted(expected):
            raise ValueError('missing/extra signal')
        cash, busy, closed = 1000., 0, []
        delay, impact = (6, .001) if scenario == 'stress' else (1, .0005)
        for record in rows:
            s = record['signal']; event = s['funding_event_ms']; t = s['T']; rate = expected[event]
            if s['funding_rate'] != rate or t != ((event+M-1)//M)*M or s['last_closed_price_ms'] != (event//(15*M))*(15*M) or s['settled_funding_is_earned']:
                raise ValueError('event known only after settlement')
            if s['side'] != (-1 if rate > 0 else 1):
                raise ValueError('contrarian side')
            if t < busy:
                if record.get('ignored') != 'position_open':
                    raise ValueError('overlap')
                continue
            if record.get('rejection') == 'notional_cap_100':
                if not record['proposed_notional'] > 100+1e-8:
                    raise ValueError('false cap rejection')
                continue
            o = record.get('outcome', {})
            if 'net' not in o:
                if not record.get('rejection') and not o.get('rejection'):
                    raise ValueError('missing result')
                continue
            if o['symbol'] != symbol or o['side'] != s['side'] or o['entry_ms'] != t+delay*M:
                raise ValueError('entry before availability')
            if not o['entry_ms'] < o['exit_ms'] <= o['entry_ms']+240*M or o['qty']*o['entry']>100+1e-8 or o['estimated_risk']>5+1e-8:
                raise ValueError('time or risk limit')
            if o['qty']*o['entry']+o['entry_fee']>cash+1e-8:
                raise ValueError('cash limit')
            if o['funding_events'] or o['funding'] != 0:
                # All actual source intervals are 8h, all allowed positions
                # start after settlement and end within4h. Nothing to earn.
                raise ValueError('unearned settled funding')
            if any(o['entry_ms'] <= ft <= o['exit_ms'] for ft in events[symbol]):
                raise ValueError('unexpected in-position funding event')
            q, side = o['qty'], o['side']
            close(o['entry'], o['entry_reference']*(1+side*impact))
            close(o['exit'], o['exit_reference']*(1-side*impact))
            close(o['fees'], q*(o['entry']+o['exit'])*.0005)
            close(o['gross'], side*q*(o['exit_reference']-o['entry_reference']))
            close(o['impact'], q*(abs(o['entry']-o['entry_reference'])+abs(o['exit']-o['exit_reference'])))
            close(o['net'], side*q*(o['exit']-o['entry'])-o['fees']-o['liquidation_fee'])
            cash += o['net']; busy = o['exit_ms']; closed.append(o)
        saved = summary['results'][key]
        if saved['closed'] != len(closed):
            raise ValueError('closed count')
        close(cash, saved['cash']); close(cash, 1000+saved['net'])
        for name in ('net', 'gross', 'fees', 'impact', 'funding', 'liquidation_fee'):
            close(saved[name], sum(o[name] for o in closed))
        for side in (-1, 1):
            if saved['directions'][str(side)] != sum(o['side'] == side for o in closed):
                raise ValueError('direction count')
        for name, lo, hi in segment_dates:
            close(saved['segments'][name], sum(o['net'] for o in closed if stamp(lo) <= o['exit_ms'] < stamp(hi)))
        close(saved['without_best'], saved['net']-max((o['net'] for o in closed), default=0))
        verified[key] = {'settled_signals': len(rows), 'closed': len(closed), 'funding_earned': 0, 'event_ledger': 'PASS'}
    for asset, ev in summary['evaluations'].items():
        b, s = summary['results'][asset+'/base'], summary['results'][asset+'/stress']
        checks = {'counts': all(v['closed'] >= 30 and min(v['directions'].values()) >= 10 for v in (b, s)),
                  'positive_base_stress': b['net'] > 0 and s['net'] > 0,
                  'stress_three_segments_positive': sum(v > 0 for v in s['segments'].values()) >= 3,
                  'stress_without_best_positive': s['without_best'] > 0,
                  'drawdown_within_2pct': max(b['drawdown'], s['drawdown']) <= .02}
        if ev != {'checks': checks, 'candidate': all(checks.values())}:
            raise ValueError('selection decision')
    if summary['trading_admitted'] or summary['fits']:
        raise ValueError('unexpected admission')
    result = {'status': 'PASS', 'actual_settled_source_events': 'independently reread',
              'frozen_sources': 'match', 'verified': verified, 'decisions': 'match', 'new_http': 0}
    (out/'audit.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main(Path(sys.argv[1]))
