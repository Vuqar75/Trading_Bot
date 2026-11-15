"""Read-only audit of saved paper path. No provider/credential imports or HTTP."""
import argparse,collections,datetime,hashlib,json,sqlite3
from decimal import Decimal,ROUND_DOWN
from pathlib import Path
from unittest.mock import patch

def audit(paper_path,budget_path,start,end,journal):
    from research import ai_limit_plan as p
    db=sqlite3.connect(Path(paper_path).resolve().as_uri()+'?mode=ro',uri=True)
    budget=sqlite3.connect(Path(budget_path).resolve().as_uri()+'?mode=ro',uri=True)
    requests=[];plans={}
    for raw,state in db.execute('SELECT payload,state FROM bracket_plans'):
        plan=json.loads(raw);plans[plan['provenance']['response_id']]={'id':plan['plan_id'],'direction':plan['direction'],'entry':plan['entry'],'stop':plan['stop'],'target':plan['target'],'state':json.loads(state)}
    for rid,raw,received,result,request in budget.execute('SELECT id,snapshot,received_ms,result,request FROM bracket_api_requests ORDER BY received_ms'):
        snap=json.loads(raw)
        if not start<=snap['as_of_ms']<=end:continue
        result=json.loads(result) if result else {};req=json.loads(request)
        count=collections.Counter(x['direction'] for x in snap.get('candidate_plans',[]));decision=result.get('decision')
        record={'request_id':rid,'snapshot_id':snap['snapshot_id'],'asof_ms':snap['as_of_ms'],'received_ms':received,'long':count['LONG'],'short':count['SHORT'],'decision':decision,'admission':result.get('admission'),'usage':result.get('usage'),'request_sha256':hashlib.sha256(request.encode()).hexdigest(),'snapshot_request_bound':any(x.get('role')=='user' and json.loads(x['content'])==snap for x in req.get('input',[])),'plan':plans.get(rid)}
        inp=db.execute('SELECT payload FROM bracket_inputs WHERE id=?',('model:'+rid,)).fetchone()
        record['paper_input_present']=bool(inp)
        # Install was 15 Nov03:45:24 UTC. Older snapshots retain the old generator.
        if snap['as_of_ms']>=1794714324000 and inp:
            env=json.loads(inp[0]);cap=env['capture'];stats=collections.defaultdict(collections.Counter);rank=collections.defaultdict(int);original=p.validate
            def check(s,d,now):
                direction=d['decision'];entry=next(x for x in s['levels'] if x['id']==d['entry_level']);side=1 if direction=='LONG' else -1
                targets=sorted([x for x in s['levels'] if x['kind']==('resistance' if side==1 else 'support') and side*(Decimal(x['price'])-Decimal(entry['price']))>0],key=lambda x:(abs(Decimal(x['price'])-Decimal(entry['price'])),x['id']))
                unique=[];seen=set()
                for x in targets:
                    if x['price'] not in seen:unique.append(x);seen.add(x['price'])
                r=next(i+1 for i,x in enumerate(unique) if x['id']==d['target_level'])
                stats[direction]['tested']+=1;rank[direction+'_tested']=max(rank[direction+'_tested'],r)
                try:value=original(s,d,now)
                except ValueError as e:stats[direction][str(e)]+=1;raise
                stats[direction]['passed']+=1;rank[direction+'_passed']=max(rank[direction+'_passed'],r);return value
            with patch.object(p,'validate',side_effect=check):recomputed=p.context(cap,snap['quote'],snap['filters'],snap['news'],snap['as_of_ms'])
            checks=[]
            for candidate in snap.get('candidate_plans',[]):
                d={'snapshot_id':snap['snapshot_id'],'decision':candidate['direction'],'entry_level':candidate['entry_level'],'stop_level':candidate['stop_level'],'target_level':candidate['target_level'],'valid_until_ms':snap['expires_ms']-1,'max_hold_minutes':240,'thesis':'causal audit','news_ids':[],'risks':[]}
                v=original(snap,d,snap['as_of_ms']);side=Decimal(1 if candidate['direction']=='LONG' else -1);entry,stop,target=map(Decimal,[v['entry'],v['stop'],v['target']]);stop_fill=stop*(1-side*Decimal('.001'));loss=side*(entry-stop_fill)+Decimal('.0005')*(entry+stop_fill)+entry*Decimal('.0021');reward=side*(target-entry)-Decimal('.0005')*(entry+target)-entry*Decimal('.0021');quantity=(min(Decimal(100)/entry,Decimal(5)/loss)/Decimal(snap['filters']['step_size'])).to_integral_value(rounding=ROUND_DOWN)*Decimal(snap['filters']['step_size'])
                checks.append({'direction':candidate['direction'],'entry':v['entry'],'stop':v['stop'],'target':v['target'],'qty':v['quantity'],'risk':v['risk_usdt'],'rr':v['net_reward_risk'],'matches':all(v[k]==candidate[k] for k in ['entry','stop','target']) and v['quantity']==candidate['quantity_cap'] and v['risk_usdt']==candidate['estimated_risk_usdt'] and format(Decimal(v['net_reward_risk']),'.4f')==candidate['net_reward_risk'],'arithmetic_ok':quantity==Decimal(v['quantity']) and quantity*loss==Decimal(v['risk_usdt']) and abs(reward/loss-Decimal(v['net_reward_risk']))<=Decimal('.000000005')})
            record['causal_check']={'context_exact':recomputed==snap,'input_snapshot_exact':env['analysis']['snapshot']==snap,'candles_sha256':p.digest(cap['candles']),'counts':dict(stats),'ranks':dict(rank),'candidates':checks}
        requests.append(record)
    messages=[json.loads(r[0]) for r in db.execute('SELECT payload FROM bracket_messages')]
    after=[m for m in messages if m.get('kind')=='DATA_GAP' and m.get('at_ms',0)>1794747086630]
    errors=db.execute("SELECT value FROM bracket_meta WHERE key='poll_error_history:BTCUSDT'").fetchone()
    public=json.load(open('/var/lib/ai-bracket-public/orders.json'))
    report={'start_ms':start,'end_ms':end,'requests':requests,'journal':journal,'post_6937809':{'gap_count_excluding_bootstrap':len(after),'gaps':after[-3:],'quote_errors_retained':json.loads(errors[0]) if errors else [],'public_asof':public['as_of_ms'],'stream':public['trade_feeds']['BTCUSDT'].get('stream'),'market_errors':public['market_errors'],'balances':public['balances'],'ledger_balanced':public['ledger_balanced'],'active':[(x['plan_id'],x['status']) for x in public['plans'] if x['status'] not in ('CLOSED','CANCELED')]}}
    db.close();budget.close();return report
