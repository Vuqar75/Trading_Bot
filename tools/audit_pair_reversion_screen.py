"""Independent ledger and protocol checks on cached paired return observations."""
import gzip
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M = 60000


def close(a, b):
    if not math.isclose(a, b, abs_tol=1e-11, rel_tol=1e-10):
        raise ValueError(f'financial mismatch: {a} != {b}')


def main():
    receipt = json.loads((ROOT/'PAIR_REVERSION_INPUTS.json').read_text())
    for name, expected in receipt['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected:
            raise ValueError('frozen file changed: '+name)
    out = ROOT/'research/pair_reversion_results'
    summary = json.loads((out/'summary.json').read_text())
    sources = json.loads((ROOT/'research/pattern_ai_results/sources.json').read_text())
    for asset, inputs in summary['sources'].items():
        original = {x['file']: x['sha256'] for x in sources[asset]}
        actual = {x['file']: x['sha256'] for x in inputs}
        if original != actual:
            raise ValueError('archive source differs from pinned original')
    raw = gzip.decompress((out/'cases.json.gz').read_bytes())
    if hashlib.sha256(raw).hexdigest() != summary['case_uncompressed_sha256']:
        raise ValueError('case hash')
    cases = json.loads(raw)
    verified = {}
    for scenario, rows in cases.items():
        impact = .001 if scenario == 'stress' else .0005
        delay = 6 if scenario == 'stress' else 1
        prior_exit = 0
        sums = dict.fromkeys(('gross', 'fees', 'impact', 'funding', 'net'), 0.)
        for row in rows:
            if row['entry_ms'] != row['signal_ms']+delay*M or row['exit_ms'] != row['entry_ms']+240*M:
                raise ValueError('label timing')
            if row['signal_ms'] < prior_exit or row['signal_ms'] % (15*M):
                raise ValueError('overlap or unclosed signal')
            prior_exit = row['exit_ms']
            if not (abs(row['previous_z']) < 2 <= abs(row['z'])):
                raise ValueError('crossing')
            close(row['z'], (row['spread']-row['prior_mean'])/row['prior_std'])
            if abs(row['spread']-row['prior_mean'])/2 < .0035 or row['eth_side'] != (-1 if row['z'] > 0 else 1):
                raise ValueError('cost-distance or direction')
            if [leg['symbol'] for leg in row['legs']] != ['ETHUSDT', 'BTCUSDT']:
                raise ValueError('leg identity')
            leg_totals = dict.fromkeys(sums, 0.)
            for leg in row['legs']:
                side, q = leg['side'], leg['qty']
                if side != (row['eth_side'] if leg['symbol'] == 'ETHUSDT' else -row['eth_side']):
                    raise ValueError('unhedged side')
                entry, exit_ = leg['entry_reference'], leg['exit_reference']
                close(q*entry, 1)
                close(leg['entry_fill_model'], entry*(1+side*impact))
                close(leg['exit_fill_model'], exit_*(1-side*impact))
                expected = {'gross': q*side*(exit_-entry),
                            'fees': .0005*q*(leg['entry_fill_model']+leg['exit_fill_model']),
                            'impact': q*(abs(entry-leg['entry_fill_model'])+abs(exit_-leg['exit_fill_model'])),
                            'funding': 0.}
                seen = set()
                for event in leg['funding_events']:
                    if not row['entry_ms'] < event['time_ms'] < row['exit_ms'] or event['time_ms'] in seen:
                        raise ValueError('funding boundary or duplicate')
                    seen.add(event['time_ms'])
                    close(event['amount'], -side*q*event['mark']*event['rate'])
                    expected['funding'] += event['amount']
                expected['net'] = side*q*(leg['exit_fill_model']-leg['entry_fill_model'])-expected['fees']+expected['funding']
                for k, value in expected.items():
                    close(leg[k], value)
                    leg_totals[k] += value
            for k in sums:
                close(row[k], leg_totals[k])
                sums[k] += leg_totals[k]
            close(row['net_bps_gross_notional'], row['net']*5000)
        saved = summary['results'][scenario]
        if len(rows) != saved['pairs']:
            raise ValueError('pair count')
        for k in sums:
            close(saved['sums_per_unit_pair'][k], sums[k])
        if rows:
            close(saved['mean_net_bps'], sums['net']*5000/len(rows))
            close(saved['net_without_best_pair'], sums['net']-max(r['net'] for r in rows))
        for side in (-1, 1):
            if saved['direction_counts'][str(side)] != sum(row['eth_side'] == side for row in rows):
                raise ValueError('direction count')
        # Dates are parsed independently, without importing the replay module.
        import datetime as dt
        def stamp(date):
            return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
        for name, start, end in [('2025H1', '2025-01-01', '2025-07-01'), ('2025H2', '2025-07-01', '2026-01-01'),
                                 ('2026H1', '2026-01-01', '2026-07-01'), ('2026H2', '2026-07-01', '2026-11-01')]:
            subset = [row for row in rows if stamp(start) <= row['exit_ms'] < stamp(end)]
            if saved['segments'][name]['pairs'] != len(subset):
                raise ValueError('segment count')
            close(saved['segments'][name]['sum_net'], sum(row['net'] for row in subset))
        verified[scenario] = {'pairs': len(rows), 'both_leg_accounting': 'PASS', 'timing_and_nonoverlap': 'PASS'}
    r = summary['results']
    checks = {'counts_and_sides': all(v['pairs'] >= 60 and min(v['direction_counts'].values()) >= 10 for v in r.values()),
              'both_mean_net_positive': all(v['mean_net_bps'] is not None and v['mean_net_bps'] > 0 for v in r.values()),
              'stress_three_segments_positive': sum(v['sum_net'] > 0 for v in r['stress']['segments'].values()) >= 3,
              'stress_without_best_positive': r['stress']['net_without_best_pair'] > 0}
    if checks != summary['checks'] or all(checks.values()) != summary['screen_pass']:
        raise ValueError('decision mismatch')
    if summary['trading_admitted'] or summary['actual_executions'] or summary['fitted_models']:
        raise ValueError('unsupported admission')
    result = {'status': 'PASS', 'frozen_inputs': 'match', 'cases_sha256': 'match', 'verified': verified,
              'screen_pass': all(checks.values()), 'new_requests': 0}
    (out/'audit.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
