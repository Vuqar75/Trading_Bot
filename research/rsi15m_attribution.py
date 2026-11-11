"""Frozen two-factor diagnosis, not a strategy-selection experiment.

Retain the prior diagonal results. Calculate only the two missing cells:
1-minute delay / 10bp impact and 6-minute delay / 5bp impact. All signal rules,
fees, size and protection remain frozen. This cannot qualify a rejected rule.
"""
import gzip
import json
from pathlib import Path
import sys
from research import rsi15m_verification as r


def main(root, out):
    r.configure_period()
    Base, account_sha = r.load_account_class()
    Protected = r.protected_class(Base)
    trade, mark, funding, proof, conflicts = r.core.load_data(root, 'BTCUSDT')
    assert not conflicts
    candidates = r.signals(trade)
    fby = {}
    for t, rate in funding:
        fby.setdefault((t-r.START)//r.M, []).append((t, rate))
    results, journals = {}, {}
    for delayed, impact in ((False, .001), (True, .0005)):
        a = Protected(delayed)
        a.impact = impact
        name = f'delay{6 if delayed else 1}_impact{int(impact*10000)}'
        for i in range((r.BEGIN-r.START)//r.M, len(trade)):
            t = r.START+i*r.M
            if i % 15 == 0:
                sig = candidates[i//15-1]
                side = sig['side']
                if side and not (a.last_exit_side == side and sig['bar_index']-a.last_exit_bar < 6):
                    a.pending_source = sig
                    a.signal(t, side, None)
            if a.position or a.pending:
                a.tick(t, trade[i], mark[i], fby.get(i, []))
            assert not a.halted
        results[name] = a.finish(float(mark[-1, 3]))
        assert not a.position and not a.pending
        assert abs(a.cash-1000-sum(x['net'] for x in a.trades)) < 1e-7
        journals[name] = a.trades
        print(name, results[name]['net_including_open'], flush=True)
    baseline = json.loads((out/'summary.json').read_text())['results']
    results['delay1_impact5'] = baseline['base']
    results['delay6_impact10'] = baseline['stress']
    net = {k:v['net_including_open'] for k,v in results.items()}
    # Symmetric (Shapley) allocation avoids attributing interaction twice.
    delay = ((net['delay6_impact5']-net['delay1_impact5'])+
             (net['delay6_impact10']-net['delay1_impact10']))/2
    impact = ((net['delay1_impact10']-net['delay1_impact5'])+
              (net['delay6_impact10']-net['delay6_impact5']))/2
    assert abs(delay+impact-(net['delay6_impact10']-net['delay1_impact5'])) < 1e-8
    payload = {'purpose':'diagnosis only; no admission or parameter selection',
               'results':results,'symmetric_change_allocation':{'delay_usdt':delay,'impact_usdt':impact},
               'account_source_sha256':account_sha,'checks':['cash reconciliation','no unresolved or residual','allocation reconciles']}
    (out/'attribution.json').write_text(json.dumps(payload, indent=2)+'\n')
    with gzip.open(out/'attribution_trades.json.gz','wt') as f:
        json.dump(journals,f,sort_keys=True)


if __name__ == '__main__':
    main(Path(sys.argv[1]),Path(sys.argv[2]))
