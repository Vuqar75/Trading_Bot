"""Audit stored EMA selection, exposure transitions and financial ledgers."""
import gzip,hashlib,json,math,pathlib,sys
root=pathlib.Path(sys.argv[1]);summary=json.loads((root/'summary.json').read_text(encoding='utf8'))
def read(name):
 with gzip.open(root/(name+'.json.gz'),'rt',encoding='utf8') as f:return json.load(f)
choices=read('choices');signals=read('signals');events=read('events');ledger=read('ledger')
for c in choices:
 assert c['train_start_ms']<c['train_end_ms']==c['last_training_close_ms']==c['test_start_ms']<c['test_end_ms']
 assert c['first_training_close_ms']==c['train_start_ms']+3600000
 assert c['train_end_ms']-c['train_start_ms']==(14 if c['schedule']=='14d_10d' else 7)*24*3600000
 if c['selected_pair'] is not None:assert c['selected_pair'][0] in (5,7,10,15,20,30) and c['selected_pair'][1] in (40,50,100,150,200)
same_exposure_pair_switches=0
for symbol in ('BTCUSDT','ETHUSDT'):
 for sched in ('14d_10d','7d_28d'):
  cs=[x for x in choices if x['symbol']==symbol and x['schedule']==sched]
  assert all(a['test_end_ms']==b['test_start_ms'] for a,b in zip(cs,cs[1:]))
  rows=signals[f'{symbol}/{sched}/base'];prev=0;turn=0;previous_pair=None
  for x in rows:
   turn+=abs(x['desired']-prev);prev=x['desired']
   if x['action']=='hold':assert x['desired']==x['position']
   if previous_pair is not None and x['selected_pair']!=previous_pair and x['position']!=0 and x['desired']==x['position']:
    assert x['action']=='hold';same_exposure_pair_switches+=1
   previous_pair=x['selected_pair']
  diag=summary['diagnostic_A'][f'{symbol}/{sched}'];assert diag['physical_turnover_units']==turn
  assert math.isclose(diag['actual_transition_cost'],turn*.001,abs_tol=1e-8)
  assert math.isclose(diag['log_net_after_final_close'],diag['log_net_before_final_close']-.001*abs(prev),abs_tol=1e-8)
total=0;reversal_pairs=0
for key,rows in ledger.items():
 z=summary['futures_B'][key];total+=len(rows);rows=sorted(rows,key=lambda x:x['entry_ms'])
 for a,b in zip(rows,rows[1:]):assert a['exit_ms']<=b['entry_ms']
 for r in rows:
  delay=6*60000 if key.endswith('/stress') else 60000
  assert r['entry_ms']==r['signal_ms']+delay and r['estimated_risk']<=5 and r['qty']*max(r['entry'],r['mark_entry'])<=100+1e-8
  assert math.isclose(r['gross']-r['fees']-r['impact']-r['liquidation_fee']+r['funding'],r['net'],abs_tol=1e-8)
  assert math.isclose(r['entry_fee']+r['exit_fee'],r['fees'],abs_tol=1e-9)
  assert math.isclose(sum(x['applied_usdt'] for x in r['funding_events']),r['funding'],abs_tol=1e-9)
  assert all(r['entry_ms']<=x['event_ms']<r['exit_ms'] for x in r['funding_events'])
 assert len(rows)==z['closed'] and math.isclose(sum(x['net'] for x in rows),z['net_closed'],abs_tol=1e-8)
 open_=z['open'];expected=1000+z['net_closed']+(-open_['entry_fee']+open_['funding'] if open_ else 0.)
 if open_:
  assert open_['estimated_risk']<=5 and open_['qty']*max(open_['entry'],open_['mark_entry'])<=100+1e-8
  assert not rows or rows[-1]['exit_ms']<=open_['entry_ms']
  assert math.isclose(sum(x['applied_usdt'] for x in open_['funding_events']),open_['funding'],abs_tol=1e-9)
  assert all(x['event_ms']>=open_['entry_ms'] for x in open_['funding_events'])
 assert math.isclose(z['cash'],expected,abs_tol=1e-7)
 if not z['unresolved']:
  eq=1000+z['net_closed']+(open_['unrealized_net_estimate'] if open_ else 0.)
  assert math.isclose(z['equity'],eq,abs_tol=1e-7)
 for a,b in zip(rows,rows[1:]):
  if a['reason'] in ('stop','liquidation'):assert b['signal_ms']>a['exit_ms']
 e=events[key]
 for i,x in enumerate(e):
  if x['action']=='exit' and x['reason']=='signal_reversal' and i+1<len(e) and e[i+1]['action']=='entry' and e[i+1]['time_ms']==x['time_ms']:
   assert x['fee']>0 and e[i+1]['fee']>0;reversal_pairs+=1
files=['summary.json','choices.json.gz','signals.json.gz','events.json.gz','ledger.json.gz']
out={'choices':len(choices),'signals':sum(map(len,signals.values())),'closed_audited':total,'verified_reversal_double_fills':reversal_pairs,'verified_same_exposure_pair_switch_holds_base':same_exposure_pair_switches,
 'sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files},
 'checks':'chronological selection; stitched turnover; same-exposure holds; 2-sided reversal fees; quantity, risk, funding and ledger'}
(root/'audit.json').write_text(json.dumps(out,indent=2,sort_keys=True)+'\n',encoding='utf8');print(json.dumps(out,indent=2))
