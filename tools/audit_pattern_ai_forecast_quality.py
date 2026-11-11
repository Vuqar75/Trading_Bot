"""Audit saved chronological forecasts against their original stress targets.

No training, threshold search, market/network call or trading state access.
Counterfactual outcomes overlap and must not be summed as account PnL.
"""
import datetime as dt,gzip,hashlib,json
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]/'research/pattern_ai_results'
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')


def profile(rows):
    if not rows:return {'examples':0}
    return {'examples':len(rows),'mean_predicted_net_bps':float(np.mean([r['predicted_net'] for r in rows])*10000),
            'mean_realized_counterfactual_net_bps':float(np.mean([r['realized_net'] for r in rows])*10000),
            'mean_predicted_p_win':float(np.mean([r['p_win'] for r in rows])),
            'actual_positive_fraction':float(np.mean([r['realized_net']>0 for r in rows])),
            'distinct_utc_days':len({r['T']//86400000 for r in rows}),
            'months':len({r['fold'] for r in rows})}


def main():
    with np.load(ROOT/'training_examples.npz') as archive:
        a={k:archive[k] for k in ('T','symbol','stress_target','label_end_ms')}
    lookup={(int(t),int(s)):i for i,(t,s) in enumerate(zip(a['T'],a['symbol']))}
    assert len(lookup)==len(a['T'])
    predictions=json.load(gzip.open(ROOT/'predictions.json.gz','rt'))
    folds={f['test_month']:f for f in json.loads((ROOT/'summary.json').read_text())['folds']}
    seen=set();rows=[]
    for p in predictions:
        key=(p['T'],SYMBOLS.index(p['symbol']))
        assert key not in seen;seen.add(key);i=lookup[key]
        f=folds[p['fold']]
        assert f['max_train_label_end_ms']<f['test_start_ms']<=p['T']<f['test_end_ms']
        target=float(a['stress_target'][i])
        if not np.isfinite(target):raise ValueError('unresolved_counterfactual_target')
        rows.append({'T':p['T'],'symbol':p['symbol'],'fold':p['fold'],'predicted_net':p['pred_net'],'p_win':p['p_win'],'realized_net':target})
    # This component of the original entry gate was frozen BEFORE fitting.
    chosen=[r for r in rows if r['predicted_net']>=.0005]
    chosenkeys={(r['T'],r['symbol']) for r in chosen}
    full=ROOT/'examples.json.gz';casefile=ROOT/'forecast_quality_cases.json.gz'
    if full.exists():
        raw=gzip.open(full,'rb').read()
        expected=json.loads((ROOT/'audit.json').read_text())['input_receipt']['uncompressed_examples_sha256']
        assert hashlib.sha256(raw).hexdigest()==expected
        cases=[{'T':r['T'],'symbol':r['symbol'],'stress':r['stress']} for r in json.loads(raw) if (r['T'],r['symbol']) in chosenkeys]
        with gzip.open(casefile,'wt') as stream:json.dump(cases,stream,separators=(',',':'),sort_keys=True)
    else:cases=json.load(gzip.open(casefile,'rt'))
    casekeys={(c['T'],c['symbol']) for c in cases};assert casekeys==chosenkeys and len(cases)==len(casekeys)
    costs=[]
    for case in cases:
        z=case['stress'];i=lookup[(case['T'],SYMBOLS.index(case['symbol']))]
        net=z['gross']-z['fees']-z['impact']+z['funding']-z['liquidation_fee']
        assert abs(net-z['net'])<1e-8 and abs(net/z['notional']-a['stress_target'][i])<1e-10
        costs.append({k:z[k]/z['notional']*10000 for k in ('gross','fees','impact','funding','liquidation_fee','net')})
    result={'unit':'overlapping counterfactual forecast examples; not actual trades or a shared account',
            'selection':'original predicted stress-net >=5bp, unchanged; diagnostic only, no new admission gate',
            'all_forecasts':profile(rows),'original_regressor_gate':profile(chosen),
            'selected_cost_decomposition_mean_bps':{k:float(np.mean([c[k] for c in costs])) for k in costs[0]},
            'by_asset':{s:profile([r for r in chosen if r['symbol']==s]) for s in SYMBOLS},
            'by_month':{m:profile([r for r in chosen if r['fold']==m]) for m in folds},
            'maximum_p_win':max(r['p_win'] for r in rows),'passed_original_combined_gate':sum(r['p_win']>=.6 and r['predicted_net']>=.0005 for r in rows),
            'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('training_examples.npz','predictions.json.gz','summary.json')},
            'limitations':['Historical data and model family already used for development.','Examples overlap within4h and across assets; counts are not independent sample size.','No confidence intervals or proof of live execution claimed.','These results evaluate local RandomForest,not the single live GPT regime answer.']}
    (ROOT/'forecast_quality.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('all_forecasts','original_regressor_gate','selected_cost_decomposition_mean_bps','by_asset')},indent=2))

if __name__=='__main__':main()
