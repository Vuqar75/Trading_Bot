"""Saved-slice diagnostic; reuses generator and financial validation, no HTTP."""
import collections
from decimal import Decimal
from unittest.mock import patch
from research import ai_limit_plan as p

def key(c):return tuple(c[k] for k in ('direction','entry_level','stop_level','target_level'))
def crossing(snapshot,candidate):
    side=1 if candidate['direction']=='LONG' else -1
    entry,target=Decimal(candidate['entry']),Decimal(candidate['target'])
    return sorted({x['price'] for x in snapshot['levels'] if x['kind']==('resistance' if side==1 else 'support') and side*(Decimal(x['price'])-entry)>0 and side*(target-Decimal(x['price']))>0},key=Decimal)
def compare(snapshot,pool):
    offered={key(c) for c in snapshot['candidate_plans']};out=[]
    for candidate in snapshot['candidate_plans']:
        near=[c for c in pool if c['direction']==candidate['direction'] and c['entry_level']==candidate['entry_level'] and c['stop_level']==candidate['stop_level'] and abs(Decimal(c['target'])-Decimal(c['entry']))<abs(Decimal(candidate['target'])-Decimal(candidate['entry'])) and key(c) not in offered]
        out.append({'offered':candidate,'intermediate_prices':crossing(snapshot,candidate),'nearer_valid_omitted':sorted(near,key=lambda c:abs(Decimal(c['target'])-Decimal(c['entry'])))})
    return out

def analyse(envelope,*,plan_module=None):
    module=plan_module or p
    snap=envelope['analysis']['snapshot'];cap=envelope['capture'];pool=[];failures=[];nearest={};original=module.validate
    def capture(s,d,now):
        mapped={x['id']:Decimal(x['price']) for x in s['levels']}
        group=(d['decision'],d['entry_level'],d['stop_level'])
        distance=abs(mapped[d['target_level']]-mapped[d['entry_level']])
        def note(reason):
            if group not in nearest or distance<Decimal(nearest[group]['distance']):
                nearest[group]={'direction':d['decision'],'entry_level':d['entry_level'],'stop_level':d['stop_level'],'target_level':d['target_level'],'raw_target':str(mapped[d['target_level']]),'distance':str(distance),'outcome':reason}
        try:checked=original(s,d,now)
        except ValueError as e:
            note(str(e));failures.append({'direction':d['decision'],'entry_level':d['entry_level'],'stop_level':d['stop_level'],'target_level':d['target_level'],'reason':str(e)});raise
        note('passed')
        pool.append({'direction':d['decision'],**{k:d[k] for k in ('entry_level','stop_level','target_level')},**{k:checked[k] for k in ('entry','stop','target','quantity','risk_usdt','net_reward_risk')}})
        return checked
    with patch.object(module,'validate',side_effect=capture):rebuilt=module.context(cap,snap['quote'],snap['filters'],snap['news'],snap['as_of_ms'])
    if rebuilt!=snap:raise ValueError('context_reconstruction_mismatch')
    compared=compare(snap,pool);summary={}
    for direction in ('LONG','SHORT'):
        selected=[x for x in compared if x['offered']['direction']==direction]
        passed=[c for c in pool if c['direction']==direction]
        summary[direction]={'passing_variants':len(passed),'offered':len(selected),'distinct_entries_offered':len({c['offered']['entry'] for c in selected}),'offered_with_intermediate_levels':sum(bool(c['intermediate_prices']) for c in selected),'offered_with_nearer_valid_omitted':sum(bool(c['nearer_valid_omitted']) for c in selected),'distinct_omitted_nearer_targets':len({key(c) for x in selected for c in x['nearer_valid_omitted']}),'passing_without_intermediate':sum(not crossing(snap,c) for c in passed),'offered_with_clean_nearer_omitted':sum(any(not crossing(snap,c) for c in x['nearer_valid_omitted']) for x in selected)}
    return {'snapshot_id':snap['snapshot_id'],'asof_ms':snap['as_of_ms'],'request_id':envelope['analysis']['provenance']['response_id'],'capture_sha256':p.digest(cap),'source_candles_sha256':snap['source_candles_sha256'],'context_exact':True,'model_decision':envelope['analysis']['decision'],'summary':summary,'comparison':compared,'pool':pool,'rejections':failures,'nearest_raw_targets':list(nearest.values())}

def enrich(result,snapshot,request_snapshot):
    if request_snapshot!=snapshot:raise ValueError('saved_request_context_mismatch')
    result['model_input_exact']=True
    result['levels_as_sent']=request_snapshot['levels']
    result['frames_as_sent']=request_snapshot['timeframes']
    result['quote_as_sent']=request_snapshot['quote']
    offered={key(c) for c in snapshot['candidate_plans']}
    result['excluded']=[c for c in result['pool'] if key(c) not in offered]
    for c in result['comparison']:
        c['obstacles_as_sent']=[z for z in request_snapshot['levels'] if z['price'] in c['intermediate_prices'] and z['kind']==('resistance' if c['offered']['direction']=='LONG' else 'support')]
        c['obstacles_confirmed_before_decision']=all(z['confirmed_ms']<=snapshot['as_of_ms'] for z in c['obstacles_as_sent'])
    for direction in ('LONG','SHORT'):
        selected=[c for c in snapshot['candidate_plans'] if c['direction']==direction]
        result['summary'][direction]['excluded']=sum(c['direction']==direction for c in result['excluded'])
        result['summary'][direction]['distinct_entries_pool']=len({c['entry'] for c in result['pool'] if c['direction']==direction})
        result['summary'][direction]['distinct_entry_stop_offered']=len({(c['entry'],c['stop']) for c in selected})
    return result
