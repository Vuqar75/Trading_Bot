"""Offline paired candidate comparison at the same saved decision snapshots."""
import json
from decimal import Decimal
from pathlib import Path
import sys

from . import ai_limit_plan as plan
from . import ai_limit_retest as retest


def compare(snapshot, answer, capture):
    args = capture, snapshot['quote'], snapshot['filters'], snapshot['news'], snapshot['as_of_ms']
    old = plan.context(*args, candidate_policy=snapshot['candidate_policy'])
    assert old == snapshot
    proposed = plan.context(*args, candidate_policy=retest.POLICY)
    payload = plan.request(proposed)
    assert Decimal(len(plan.canonical(payload).encode()) + 4096)/10 + 1000 <= 3640
    identity = lambda c: tuple(c[k] for k in ('entry_level','stop_level','target_level'))
    previous = {identity(c) for c in old['candidate_plans']}
    new = proposed['candidate_plans']
    for c in new:
        decision = dict(snapshot_id=proposed['snapshot_id'], decision=c['direction'],
                        entry_level=c['entry_level'], stop_level=c['stop_level'], target_level=c['target_level'],
                        valid_until_ms=proposed['expires_ms'], max_hold_minutes=60, thesis='offline comparison',
                        news_ids=[], risks=[])
        allowed = plan.validate(proposed,decision,proposed['as_of_ms'])
        assert allowed['hold_ms']==3600000 and allowed['expires_ms']==proposed['expires_ms']
        assert all(allowed[k]==c[k] for k in ('entry','stop','target'))
        for field, value in [('max_hold_minutes',59),('valid_until_ms',proposed['expires_ms']-1)]:
            try:
                plan.validate(proposed,dict(decision,**{field:value}),proposed['as_of_ms'])
            except ValueError as error:
                assert str(error)=='candidate_execution_window'
            else:
                raise AssertionError(field)
    return dict(source=old['snapshot_id'], candle_hash=old['source_candles_sha256'],
                as_of_ms=old['as_of_ms'], entry_deadline=old['expires_ms'], old=old['candidate_plans'],
                old_decision=answer['decision'], old_hold=answer['max_hold_minutes'],
                new=new, overlap=sum(identity(c) in previous for c in new),
                chosen_levels_still_offered=any(identity(c)==identity(answer) for c in new))


def main(directory):
    def read(p): return json.loads(p.read_text(encoding='utf-8-sig'))
    caps=read(directory/'nov20-sources.json')
    old=read(Path('research/PAPER_DAY1_20261118.json'))
    raw=read(directory/'nov20-raw.json')
    pairs=[(c['snapshot'],c['decision']) for c in old['cycles']]
    pairs.extend((json.loads(q['snapshot']),json.loads(q['result'])['decision'])
                 for q in raw['budget']['requests'] if q['result'])
    rows=[compare(s,a,caps[s['snapshot_id']]) for s,a in pairs]
    return rows


if __name__=='__main__':
    rows=main(Path(sys.argv[1]))
    Path('research/RETEST_60M_COMPARISON.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf8')
    for r in rows:
        print(r['as_of_ms'],len(r['old']),len(r['new']),r['overlap'],r['chosen_levels_still_offered'])
