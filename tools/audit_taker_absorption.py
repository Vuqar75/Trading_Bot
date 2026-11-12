"""Independent cached setup/closed-fill accounting. No training or HTTP."""
import gzip
import hashlib
import json
import math
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M = 60000
BAR = 15*M


def close(a, b):
    if not math.isclose(a, b, abs_tol=1e-8, rel_tol=1e-9):
        raise ValueError(f'accounting mismatch {a} vs {b}')


def main():
    receipt = json.loads((ROOT/'TAKER_ABSORPTION_INPUTS.json').read_text())
    for path, expected in receipt['sha256'].items():
        if hashlib.sha256((ROOT/path).read_bytes()).hexdigest() != expected:
            raise ValueError('registered input changed: '+path)
    out = ROOT/'research/taker_absorption_results'
    summary = json.loads((out/'summary.json').read_text())
    for path, expected in summary['artifacts_sha256'].items():
        if hashlib.sha256((out/path).read_bytes()).hexdigest() != expected:
            raise ValueError('artifact checksum')
    sources = json.loads((ROOT/'research/pattern_ai_results/sources.json').read_text())
    for asset, inputs in summary['sources'].items():
        if {x['file']: x['sha256'] for x in inputs} != {x['file']: x['sha256'] for x in sources[asset]}:
            raise ValueError('pinned source mismatch')
    journals = json.load(gzip.open(out/'journal.json.gz', 'rt'))
    setups = json.load(gzip.open(out/'setups.json.gz', 'rt'))
    confirmed = {}
    setup_count, fill_count = 0, 0
    for asset, rows in setups.items():
        confirmed[asset] = []
        if len({s['T'] for s in rows}) != len(rows) or len(rows) != summary['setup_counts'][asset]:
            raise ValueError('setup identity/count')
        for s in rows:
            setup_count += 1
            if not s['T'] == s['setup_ms']+BAR or s['T'] % BAR:
                raise ValueError('confirmation timing')
            close(s['imbalance'], (2*s['taker_buy_volume']-s['volume'])/s['volume'])
            if not (0 <= s['taker_buy_volume'] <= s['volume'] and s['volume'] >= 1.5*s['previous_volume_mean'] and
                    abs(s['imbalance']) >= .2 and abs(s['setup_close']-s['setup_open']) <= .25*s['prior_atr']):
                raise ValueError('setup threshold')
            close(s['atr_pct'], s['prior_atr']/s['setup_close'])
            if s['side'] != (-1 if s['imbalance'] > 0 else 1):
                raise ValueError('flow direction')
            expected = s['confirmation_close'] < s['setup_low'] if s['side'] == -1 else s['confirmation_close'] > s['setup_high']
            if expected != s['confirmed']:
                raise ValueError('confirmation uses close')
            if expected:
                confirmed[asset].append(s)
        if len(confirmed[asset]) != summary['confirmed_counts'][asset]:
            raise ValueError('confirmation count')
    checks_by_scenario = {}; all_stress = []
    def stamp(date):
        return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
    segments = [('2025H1', '2025-01-01', '2025-07-01'), ('2025H2', '2025-07-01', '2026-01-01'),
                ('2026H1', '2026-01-01', '2026-07-01'), ('2026H2', '2026-07-01', '2026-11-01')]
    for key, rows in journals.items():
        asset, scenario = key.split('/')
        impact, delay = (.001, 6) if scenario == 'stress' else (.0005, 1)
        if [r['signal'] for r in rows] != confirmed[asset]:
            raise ValueError('missing/extra signal')
        cash, busy, closed = 1000., 0, []
        for record in rows:
            s = record['signal']
            if s['T'] < busy:
                if record.get('ignored') != 'position_open':
                    raise ValueError('overlapping admission')
                continue
            if 'ignored' in record:
                raise ValueError('false occupied state')
            if record.get('rejection') == 'notional_cap_100':
                if not record['proposed_notional'] > 100+1e-8:
                    raise ValueError('false notional rejection')
                continue
            outcome = record.get('outcome', {})
            if 'net' not in outcome:
                if not record.get('rejection') and 'rejection' not in outcome:
                    raise ValueError('missing outcome')
                continue
            fill_count += 1
            q, side = outcome['qty'], outcome['side']
            if outcome['symbol'] != asset or side != s['side'] or outcome['signal_ms'] != s['T']:
                raise ValueError('outcome binding')
            if outcome['entry_ms'] != s['T']+delay*M or not outcome['entry_ms'] < outcome['exit_ms'] <= outcome['entry_ms']+240*M:
                raise ValueError('fill timing')
            if q*outcome['entry'] > 100+1e-8 or outcome['estimated_risk'] > 5+1e-8:
                raise ValueError('risk/notional')
            if q*outcome['entry']+outcome['entry_fee'] > cash+1e-8:
                raise ValueError('cash')
            step = .01 if asset == 'SOLUSDT' else .001
            close(q/step, round(q/step))
            stop_pct = min(.02, max(.005, 1.5*s['atr_pct']))
            close(outcome['stop_pct'], stop_pct)
            close(outcome['estimated_risk'], q*outcome['entry']*(stop_pct+2*(.0005+impact)+.0021))
            close(outcome['entry'], outcome['entry_reference']*(1+side*impact))
            close(outcome['exit'], outcome['exit_reference']*(1-side*impact))
            close(outcome['fees'], q*(outcome['entry']+outcome['exit'])*.0005)
            close(outcome['gross'], q*side*(outcome['exit_reference']-outcome['entry_reference']))
            close(outcome['impact'], q*(abs(outcome['entry']-outcome['entry_reference'])+abs(outcome['exit']-outcome['exit_reference'])))
            funding = 0.; seen = set()
            for event in outcome['funding_events']:
                if event['event_ms'] in seen or not outcome['entry_ms'] <= event['event_ms'] <= outcome['exit_ms']:
                    raise ValueError('funding timing')
                seen.add(event['event_ms'])
                raw = -side*q*event['mark']*event['rate']
                close(event['applied'], min(raw, 0) if event['ambiguous'] else raw)
                funding += event['applied']
            close(outcome['funding'], funding)
            direct = q*side*(outcome['exit']-outcome['entry'])-outcome['fees']+funding-outcome['liquidation_fee']
            close(outcome['net'], direct)
            cash += direct; busy = outcome['exit_ms']; closed.append(outcome)
        saved = summary['results'][key]
        if len(closed) != saved['closed']:
            raise ValueError('closed count')
        close(cash, saved['cash'])
        close(cash, 1000+saved['net'])
        for k in ('gross', 'fees', 'impact', 'funding', 'liquidation_fee', 'net'):
            close(saved[k], sum(x[k] for x in closed))
        for side in (-1, 1):
            if saved['directions'][str(side)] != sum(x['side'] == side for x in closed):
                raise ValueError('side count')
        close(saved['without_best'], saved['net']-max((x['net'] for x in closed), default=0))
        for name, lo, hi in segments:
            close(saved['segments'][name], sum(x['net'] for x in closed if stamp(lo) <= x['exit_ms'] < stamp(hi)))
        if not math.isfinite(saved['drawdown']) or saved['drawdown'] < 0:
            raise ValueError('drawdown domain')
        if scenario == 'stress':
            all_stress.extend(closed)
        checks_by_scenario[key] = {'signals': len(rows), 'closed': len(closed), 'ledger_and_binding': 'PASS'}
    results = summary['results']
    criteria = {'counts_and_sides': all(v['closed'] >= 30 and min(v['directions'].values()) >= 10 for v in results.values()),
                'all_asset_scenarios_positive': all(v['net'] > 0 for v in results.values()),
                'drawdown_within_2pct': all(v['drawdown'] <= .02 for v in results.values()),
                'stress_three_segments_positive': sum(sum(x['net'] for x in all_stress if stamp(lo) <= x['exit_ms'] < stamp(hi)) > 0 for _, lo, hi in segments) >= 3,
                'stress_without_best_positive': sum(x['net'] for x in all_stress)-max((x['net'] for x in all_stress), default=0.) > 0}
    if criteria != summary['checks'] or all(criteria.values()) != summary['screen_pass']:
        raise ValueError('decision mismatch')
    result = {'status': 'PASS', 'setups_verified': setup_count, 'closed_records_verified': fill_count,
              'registered_source_hashes': 'match', 'artifact_hashes': 'match', 'scenarios': checks_by_scenario,
              'decision_matches': True, 'training_or_http_requests': 0}
    if summary['trading_admitted'] is not False or summary['fits'] != 0:
        raise ValueError('unsupported qualification')
    (out/'audit.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
