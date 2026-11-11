"""Independent journal arithmetic and temporal audit; no market or network calls."""
import gzip
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'research/rsi15m_verification_results'
s = json.loads((ROOT/'summary.json').read_text())
trades = json.load(gzip.open(ROOT/'trades.json.gz', 'rt'))
checks = {}
for name, rows in trades.items():
    previous_exit = 0
    for r in rows:
        def equal(a, b):
            assert math.isclose(a, b, abs_tol=1e-8), (name, r['id'], a, b)
        equal(r['gross'], r['qty']*r['sign']*(r['exit_reference']-r['entry_reference']))
        equal(r['fees'], r['entry_fee']+r['exit_fee'])
        equal(r['net'], r['gross']-r['fees']-r['impact']+r['funding']-r['liquidation_fee'])
        equal(r['funding'], sum(x['applied_usdt'] for x in r['funding_events']))
        assert r['entry_ms']-r['signal_ms'] == (6 if name == 'stress' else 1)*60000
        assert previous_exit <= r['entry_ms'] < r['exit_ms']
        assert 50 <= r['entry_notional'] <= 100 and r['estimated_risk'] <= 5
        equal(r['qty']/.001, round(r['qty']/.001))
        assert all(r['entry_ms'] <= x['event_ms'] <= r['exit_ms'] for x in r['funding_events'])
        previous_exit = r['exit_ms']
    x = s['results'][name]
    assert not x['open']
    assert len(rows) == x['closed']
    equal(sum(r['net'] for r in rows), x['net_including_open'])
    checks[name] = {'audited_closed_trades': len(rows), 'arithmetic_temporal_and_limits': 'PASS',
                    'net_without_best_trade': x['net_including_open']-x['best_trade'],
                    'positive_stress_quarters': sum(q['net'] > 0 for q in x['by_segment'].values()) if name=='stress' else None}
(ROOT/'audit.json').write_text(json.dumps(checks, indent=2)+'\n')
print(json.dumps(checks, indent=2))
