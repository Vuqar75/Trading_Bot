"""One bounded release audit, only saved JSON; no network, model or executors."""
import argparse,hashlib,json,datetime
from decimal import Decimal,localcontext
from pathlib import Path
START=1794912284868
END=START+86400000
TZ=datetime.timezone(datetime.timedelta(hours=4))
def at(ms):return datetime.datetime.fromtimestamp(ms/1000,TZ).isoformat(timespec='milliseconds')
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def audit(raw,causal,rejection,baseline):
 j=load(raw);c=load(causal);r=load(rejection);base=load(baseline)
 assert j['runtime']['runtime_sha256']=='be52d3ccbd291295bd7beaa104a0dda9d3d8e3f38b2939b2276ee5fed16f06e2'
 assert j['reconcile']['balanced'] and j['events'][:28]==base['live']['events']
 usage={q['request_id']:json.loads(q['usage_json']) for q in j['budget']['usage']}
 requests={q['id']:q for q in j['budget']['requests']};models={m['id']:m for m in j['model_inputs']}
 assert len(requests)==len(j['budget']['attempts'])==len(j['budget']['usage'])==7
 launches={};published={}
 for row in j['journal']['ai-bracket-analyst.service']:
  tm=int(row['__REALTIME_TIMESTAMP'])//1000;key=at(tm)[:13]
  if row['MESSAGE'].startswith('Starting '):launches[key]=tm
  if row['MESSAGE'].startswith('{'):
   msg=json.loads(row['MESSAGE']);assert msg['network_retry'] is False;published[key]=msg
 plans=[p for p in j['plans'] if p['state']['armed_ms']>=START]
 planmap={p['payload']['provenance']['response_id']:p for p in plans};cycles=[]
 with localcontext() as ctx:
  ctx.prec=120
  for attempt in j['budget']['attempts']:
   rid=attempt['request_id'];q=requests[rid];s=json.loads(q['snapshot']);v=json.loads(q['result']);decision=v['decision'];key=at(q['received_ms'])[:13]
   assert key in launches and key in published;assert json.loads(attempt['result_json'])==v
   assert json.loads(q['raw_response'])['status']=='completed';assert usage[rid]==v['usage']
   assert models['model:'+rid]['payload']['analysis']==v
   assert published[key]['decision']==decision['decision']
   candidates={side:sum(x['direction']==side for x in s['candidate_plans']) for side in ['LONG','SHORT']}
   plan=planmap.get(rid);outcome='WAIT';reason='model_wait'
   if decision['decision']!='WAIT' and plan is None:
    saved=r['meta']['analyst_last'];assert saved['provenance']['response_id']==rid and saved['status']=='REJECTED';outcome='POST_RESPONSE_REJECTED';reason=saved['reason']
   elif plan is not None:
    outcome=plan['state']['status'];reason=(plan['state'].get('entry_cancellation') or {}).get('causes') or plan['state'].get('close_reason')
   cycles.append({'period':'first_24h' if q['received_ms']<END else 'supplement','scheduled_baku':key+':02:00+04:00','started_ms':launches[key],'received_ms':q['received_ms'],'request_id':rid,'snapshot_id':s['snapshot_id'],'candidate_policy':s['candidate_policy'],'candidates':candidates,'decision':decision,'model_admission':v['admission'],'outcome':outcome,'reason':reason,'plan_id':plan['id'] if plan else None,'usage':v['usage'],'reserve_micro_usd':v['reserved_micro_usd'],'estimated_micro_usd':v['estimated_cost_micro_usd'],'snapshot_sha256':hashlib.sha256(q['snapshot'].encode()).hexdigest(),'snapshot':s})
  assert len(launches)==len(published)==7
  trades=[]
  for p in plans:
   fs=[f for f in j['fills'] if f['plan_id']==p['id']]
   if p['state']['status']!='CLOSED':assert not fs;continue
   assert len(fs)==2 and fs[0]['qty']==fs[1]['qty'];side=Decimal(1) if p['payload']['direction']=='LONG' else Decimal(-1)
   gross=side*Decimal(fs[0]['qty'])*(Decimal(fs[1]['price'])-Decimal(fs[0]['price']));fees=sum((Decimal(f['fee']) for f in fs),Decimal(0));funding=Decimal(p['state']['funding']);net=gross-fees+funding
   assert gross==Decimal(p['state']['gross']) and fees==Decimal(p['state']['fees']);protect=p['state']['last_protective_execution'];due=protect['time_due_ms']
   trades.append({'plan_id':p['id'],'direction':p['payload']['direction'],'fills':fs,'gross':str(gross),'fees':str(fees),'funding':str(funding),'net':str(net),'hold_ms':fs[1]['stamp']-fs[0]['stamp'],'time_deadline_ms':due,'time_detection_delay_ms':protect['observed_ms']-due,'protective_execution':protect,'closed_ms':p['state']['closed_ms'],'protection_wait_history':p['state'].get('protection_wait_history',[]),'saved_quote_range':c['saved_quote_ranges'][p['id']]})
  net=sum((Decimal(t['net']) for t in trades),Decimal(0));initial=Decimal('1000.0321471104653898110');assert initial+net==Decimal(j['balances']['free'])
  deltas={}
  for e in j['events'][28:]:
   assert sum((Decimal(p['amount']) for p in e['postings']),Decimal(0))==0
   for p in e['postings']:deltas[p['account']]=deltas.get(p['account'],Decimal(0))+Decimal(p['amount'])
  assert deltas['ai_bracket:free']==net
  costs={}
  for label in ['first_24h','supplement','all']:
   rows=[v for v in cycles if label=='all' or v['period']==label];costs[label]={'calls':len(rows),'input_tokens':sum(x['usage']['input_tokens'] for x in rows),'output_tokens':sum(x['usage']['output_tokens'] for x in rows),'total_tokens':sum(x['usage']['total_tokens'] for x in rows),'estimate_usd':str(sum((Decimal(x['estimated_micro_usd']) for x in rows),Decimal(0))/1000000),'reservation_usd':str(Decimal(sum(x['reserve_micro_usd'] for x in rows))/1000000)}
  gaps=[]
  for row in j['messages']:
   g=json.loads(row['payload'])
   if g.get('kind')!='DATA_GAP':continue
   opened=[p['id'] for p in plans if p['state'].get('opened_ms') is not None and p['state']['opened_ms']<=g['at_ms']<p['state']['last_fill_ms']]
   g['open_plan_ids']=opened;gaps.append(g)
  assert len(gaps)==11 and sum(bool(g['open_plan_ids']) for g in gaps)==7
  false_receipts=[]
  for fail in j['meta']['last_gap_phase_diagnostics']['diagnostics']['quote_failures']:
   q=fail['quote_components'];b=q.get('components',{}).get('book')
   if b and 'book_receipt_time' in q['time_rejections'] and b['received_ms']>q['received_ms']:
    assert max(b['event_ms'],b['source_ms'])<=q['received_ms'];false_receipts.append({'failure_ms':fail['at_ms'],'validation_ms':q['received_ms'],'book_event_ms':b['event_ms'],'book_source_ms':b['source_ms'],'book_received_ms':b['received_ms'],'received_after_old_validation_ms':b['received_ms']-q['received_ms']})
  return {'source':{'raw_sha256':hashlib.sha256(Path(raw).read_bytes()).hexdigest(),'raw_path':str(raw),'checked_ms':j['checked_ms'],'checked_end_ms':j['checked_end_ms'],'baseline_events_28_unchanged':True},'window':{'from_ms':START,'through_ms':END},'runtime':j['runtime'],'cycles':cycles,'plans':plans,'trades':trades,'gaps':gaps,'confirmed_false_receipt_cases':false_receipts,'post_response_rejection_archive':r,'observed_quote_ranges':c['saved_quote_ranges'],'execution_input':c['execution_inputs'],'ai_costs':costs,'lifetime_reserved_micro_usd':j['budget']['all_attempts_summary']['reserved_micro_usd'],'remaining_micro_usd':250000-j['budget']['all_attempts_summary']['reserved_micro_usd'],'initial_free':str(initial),'final_free':j['balances']['free'],'net':str(net),'account_deltas':{k:str(v) for k,v in deltas.items()},'new_ledger_events':j['events'][28:],'counts':{'all_events':len(j['events']),'all_plans':len(j['plans']),'all_fills':len(j['fills']),'all_funding':len(j['funding'])},'reconcile':j['reconcile'],'current_active':[(p['id'],p['state']) for p in plans if p['state']['status'] not in ('CLOSED','CANCELED')],'public':j['public'],'services':j['services'],'backup_at_audit':j['backup'],'phase_file':j['phase_file'],'ui_render':c['ui_render'],'paper_writers':c['paper_writers'],'telegram_pollers':c['telegram_pollers'],'working_pauses':c['working_pauses']}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--raw',required=True);p.add_argument('--causal',required=True);p.add_argument('--rejection',required=True);p.add_argument('--baseline',required=True);p.add_argument('--output',required=True);a=p.parse_args();result=audit(a.raw,a.causal,a.rejection,a.baseline);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'cycles':len(result['cycles']),'closed':len(result['trades']),'net':result['net'],'costs':result['ai_costs']}))
