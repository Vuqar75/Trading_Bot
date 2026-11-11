"""One registered size intervention, no cap optimization or live state."""
from pathlib import Path
import json
import sys
from research import rsi15m_verification as r
from research import retest15m as retest


def fixed_size(price,mark,symbol,impact):
    q=.001
    if q*max(price,mark)>150:return None,'notional_cap150'
    if q*mark<50:return None,'min_notional'
    return q,None


def main(root,out):
    original=r.core.size_for
    r.core.size_for=fixed_size
    try:
        r.main(root,out/'rsi')
        retest.main(root,out/'retest')
    finally:r.core.size_for=original
    gates={}
    for name in ('rsi','retest'):
        p=out/name/'summary.json';saved=json.loads(p.read_text())
        saved['size_intervention_protocol']='SIZE_FEASIBILITY_PROTOCOL.md'
        saved['context']={'start':'2025-01-01','end_exclusive':'2026-01-01','fixed_qty_btc':.001,'notional_cap':150}
        p.write_text(json.dumps(saved,indent=2)+'\n')
        s=saved['results']
        base,stress=s['base'],s['stress']
        checks={
            'counts':all(x['closed']>=30 and x['long']>=10 and x['short']>=10 for x in s.values()),
            'positive_both':all(x['net_including_open']>0 for x in s.values()),
            'stress_without_best':stress['net_including_open']-stress['best_trade']>0,
            'three_stress_quarters':sum(q['net']>0 for q in stress['by_segment'].values())>=3,
            'drawdown':all(x['max_drawdown']<=.02 for x in s.values()),
            'intraday':all(x['fraction_at_most_24h']>=.90 for x in s.values())}
        gates[name]={'checks':checks,'pass_all':all(checks.values())}
    (out/'gates.json').write_text(json.dumps(gates,indent=2)+'\n')
    print(json.dumps(gates,indent=2),flush=True)

if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))
