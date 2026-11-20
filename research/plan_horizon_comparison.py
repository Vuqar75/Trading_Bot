"""Replay saved decisions at their original horizon, without model calls or price outcomes."""
from decimal import Decimal
from .ai_limit_plan import context, HORIZON_POLICY, validate, request, canonical, digest


def compare(snapshot, decision, capture):
    now = snapshot['as_of_ms']
    args = (capture, snapshot['quote'], snapshot['filters'], snapshot['news'], now)
    assert digest(capture['candles']) == snapshot['source_candles_sha256']
    assert context(*args, candidate_policy=snapshot['candidate_policy']) == snapshot
    proposed = context(*args, candidate_policy=HORIZON_POLICY)
    assert proposed['candidate_plans'] == snapshot['candidate_plans']
    prompt_bytes = len(canonical(request(proposed)).encode())
    assert (prompt_bytes + 4096) * Decimal('.1') + 1000 <= 3640
    hold = decision['max_hold_minutes']
    result = dict(snapshot_id=snapshot['snapshot_id'], direction=decision['decision'],
                  thesis=decision['thesis'], hold_min=hold, prompt_bytes=prompt_bytes,
                  model_input_ms=now, expiry_ms=decision['valid_until_ms'],
                  candidates=len(snapshot['candidate_plans']))
    if decision['decision'] == 'WAIT':
        validate(proposed, dict(decision, snapshot_id=proposed['snapshot_id']), now)
        return dict(result, comparison='WAIT')
    original = validate(snapshot, decision, now)
    entry, stop, target = [Decimal(original[key]) for key in ('entry','stop','target')]
    long = decision['decision'] == 'LONG'
    market = Decimal(snapshot['quote']['ask' if long else 'bid'])
    atr = Decimal(snapshot['timeframes']['15m']['atr14'])
    levels = snapshot['levels']
    obstacles = [x for x in levels if x['kind'] == ('resistance' if long else 'support')
                 and min(entry,target)<Decimal(x['price'])<max(entry,target)]
    try:
        validate(proposed, dict(decision, snapshot_id=proposed['snapshot_id']), now)
        outcome = 'pass'
    except ValueError as error:
        if str(error) not in ('target_outside_hold_envelope','hold_below_candle_resolution'):
            raise
        outcome = str(error)
    cap = proposed['excursion_q75_bps'][hold//15-1][0 if long else 1] if hold>=15 else None
    same_side = [c for c in proposed['candidate_plans'] if c['direction'] == decision['decision']]
    eligible = sum(cap is not None and abs(Decimal(c['target'])-Decimal(c['entry']))*10000
                   <= Decimal(c['entry'])*cap for c in same_side)
    return dict(result, same_side_choices=len(same_side), same_side_passing=eligible, comparison=outcome, entry=str(entry), stop=str(stop), target=str(target),
                market=str(market), atr15=str(atr), entry_pct=str(100*abs(entry-market)/market),
                entry_atr=str(abs(entry-market)/atr), target_bp=str(10000*abs(entry-target)/entry),
                cap_bp=cap,
                basis_levels=[decision[k] for k in ('entry_level','stop_level','target_level')],
                obstacles=obstacles, source_hash=snapshot['source_candles_sha256'])
