"""Run from the checkout: python -m research.paper_audit_nov20 <saved bundle directory>."""
import hashlib
import json
import sys

from decimal import Decimal as D
from pathlib import Path
from .plan_horizon_comparison import compare


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def audit(directory):
    raw, quotes, captures = [read(directory / name) for name in
                             ('nov20-raw.json','nov20-ranges.json','nov20-sources.json')]
    old = read(Path('research/PAPER_DAY1_20261118.json'))
    assert raw['events'][28:40] == old['new_ledger_events'] and raw['reconcile']['balanced']
    plans = [p for p in raw['plans'] if p['state']['armed_ms'] > raw['start_ms']]
    by_request = {p['payload']['provenance']['response_id']:p['id'] for p in plans}
    rows = []
    comparisons = []
    for c in old['cycles']:
        comparisons.append(compare(c['snapshot'],c['decision'],captures[c['snapshot_id']]))
    budget = raw['budget']
    for q in budget['requests']:
        snapshot = json.loads(q['snapshot'])
        result = json.loads(q['result']) if q['result'] else None
        reserved = next(a['reserved_micro_usd'] for a in budget['attempts'] if a['request_id']==q['id'])
        row = dict(request_id=q['id'], raw_saved=bool(q['raw_response']), reserved_micro_usd=reserved,
                   plan=by_request.get(q['id']), candidate_policy=snapshot['candidate_policy'],
                   candidates=len(snapshot['candidate_plans']), model_input_ms=snapshot['as_of_ms'])
        if result:
            comp = compare(snapshot,result['decision'],captures[snapshot['snapshot_id']])
            comparisons.append(comp)
            row.update(decision=result['decision'],usage=result['usage'],estimate_micro_usd=result['estimated_cost_micro_usd'])
        rows.append(row)
    journal=[]
    for item in raw['journal']['ai-bracket-analyst.service']:
        if item['MESSAGE'].startswith('{'):
            journal.append(dict(stamp_us=item['__REALTIME_TIMESTAMP'],**json.loads(item['MESSAGE'])))
    events=raw['events'][40:]
    for e in events:
        assert sum(D(p['amount']) for p in e['postings'])==0
    net=sum(D(p['state']['gross'])-D(p['state']['fees'])+D(p['state']['funding']) for p in plans)
    assert D(old['final_free'])+net==D(raw['balances']['free'])
    gaps=[json.loads(r['payload']) for r in raw['messages'] if json.loads(r['payload']).get('kind')=='DATA_GAP']
    return dict(checked_ms=raw['checked_ms'],runtime=raw['runtime'],services=raw['services'],
                journal=journal,requests=rows,balances=raw['balances'],reconcile=raw['reconcile'],net=str(net),
                events=events,fills=[f for f in raw['fills'] if f['plan_id'] in by_request.values()],
                plans=[dict(id=p['id'],state=p['state'],side=p['payload']['direction']) for p in plans],
                comparison=comparisons, quote_ranges=quotes['saved_quote_ranges'],telegram=quotes['ui_render'],
                current= {k:raw['public'][k] for k in ('as_of_ms','market_errors','funding_error','ledger_balanced')},
                backup_verified_ms=raw['backup']['captured_ms'],pauses=quotes['working_pauses'],
                writer_count=quotes['paper_writers'],poller_count=quotes['telegram_pollers'],
                budget_lifetime=budget['all_attempts_summary'],gaps=[dict(at_ms=g['at_ms'],
                affected=[p['id'] for p in plans if p['state']['armed_ms']<=g['at_ms']<=
                ((p['state'].get('entry_cancellation') or {}).get('at_ms') or p['state'].get('last_fill_ms') or 0)]) for g in gaps],
                hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.glob('nov20-*.json') if p.name in ('nov20-raw.json','nov20-ranges.json','nov20-sources.json')})


if __name__=='__main__':
    data=audit(Path(sys.argv[1]))
    Path('research/PAPER_HORIZON_AUDIT_20261120.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
