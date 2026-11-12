"""Attribute saved paths only; no fitting, price acquisition or live state."""
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main():
    src = ROOT / 'research/pattern_ai_results'
    raw = gzip.decompress((src / 'examples.json.gz').read_bytes())
    expected = json.loads((src / 'audit.json').read_text())['input_receipt']['uncompressed_examples_sha256']
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('examples changed')
    rows = json.loads(raw)
    assets = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
    with np.load(src / 'training_examples.npz', allow_pickle=False) as x:
        if [(r['T'], r['symbol']) for r in rows] != [(int(t), assets[int(s)]) for t, s in zip(x['T'], x['symbol'])]:
            raise ValueError('row identity mismatch')
    refs = json.load(gzip.open(src / 'predictions.json.gz', 'rt'))
    direction = ROOT / 'research/pattern_direction_results'
    summary = json.loads((direction/'summary.json').read_text())
    if hashlib.sha256((direction/'predictions.npz').read_bytes()).hexdigest() != summary['predictions_sha256']:
        raise ValueError('direction prediction hash')
    with np.load(direction/'predictions.npz', allow_pickle=False) as p:
        indices = p['source_index'].copy()
        aligned = indices[p['pred_net'] >= .0005]
    if [(rows[i]['T'], rows[i]['symbol']) for i in indices] != [(r['T'], r['symbol']) for r in refs]:
        raise ValueError('prediction pairing changed')
    groups = {'all_oof': indices, 'old_net_component_only': indices[np.array([r['pred_net'] for r in refs]) >= .0005],
              'aligned_net_component_only': aligned}
    out = {'source_uncompressed_sha256': expected, 'unit': 'mean bps per entry reference notional',
           'scope': 'same saved paths; component filters are NOT the joint probability/net admission gate',
           'zero_cost_is_resimulation': False, 'results': {}}
    for name, ix in groups.items():
        result = {'n': len(ix)}
        for scenario in ('base', 'stress'):
            values = {}
            for key in ('gross', 'fees', 'impact', 'funding', 'liquidation_fee', 'net'):
                values[key] = float(np.mean([rows[i][scenario][key] / rows[i][scenario]['notional'] * 10000 for i in ix]))
            values['same_path_zero_cost'] = values['gross'] + values['funding'] - values['liquidation_fee']
            if not np.isclose(values['net'], values['same_path_zero_cost'] - values['fees'] - values['impact'], atol=1e-10):
                raise ValueError('ledger identity')
            result[scenario] = values
        out['results'][name] = result
    path = ROOT / 'research/pattern_direction_results/cost_bound.json'
    path.write_text(json.dumps(out, indent=2) + '\n')
    print(json.dumps(out, indent=2))


if __name__ == '__main__':
    main()
