"""Independent ledger/timing audit of an already-written study journal."""
import hashlib,json,math,pathlib,sys
root=pathlib.Path(sys.argv[1]);j=(root/'journal.json').read_bytes();s=(root/'summary.json').read_bytes();journal=json.loads(j);summary=json.loads(s)['summary'];count=0
for row in journal:
 t=row['signal_ms'];assert row['sigma_last_return_close_ms']<t and row['available_ms']>t
 assert row['sigma']>0 and (row['r']<=-3*row['sigma'] if row['direction']=='LONG' else row['r']>=3*row['sigma'])
 if 'net' not in row:continue
 count+=1;assert row['entry_ms']>=row['available_ms'] and 0<row['exit_ms']-row['entry_ms']<=120*60000
 assert row['qty']*max(row['entry'],row['mark_entry'])<=100+1e-8 and row['estimated_risk']<=5
 assert math.isclose(row['entry_fee']+row['exit_fee'],row['fees'],abs_tol=1e-10)
 assert math.isclose(row['gross']-row['fees']-row['impact']-row['liquidation_fee']+row['funding'],row['net'],abs_tol=1e-9)
 assert math.isclose(sum(x['applied_usdt'] for x in row['funding_events']),row['funding'],abs_tol=1e-9)
 assert all(row['entry_ms']<=x['event_ms']<row['exit_ms'] for x in row['funding_events'])
for symbol in ('BTCUSDT','ETHUSDT'):
 for scenario in ('base','stress'):
  r=summary[f'{symbol}/{scenario}/all']; subset=[x for x in journal if x['symbol']==symbol and x['scenario']==scenario and 'net'in x]
  assert len(subset)==r['closed'] and math.isclose(sum(x['net'] for x in subset),r['net'],abs_tol=1e-8)
  assert not r['unresolved'] and math.isclose(1000+r['net'],r['ending_cash'],abs_tol=1e-8)
audit={'records':len(journal),'closed_audited':count,'journal_sha256':hashlib.sha256(j).hexdigest(),'summary_sha256':hashlib.sha256(s).hexdigest(),'checks':'signal direction, sigma causality, time, funding event timing, size, risk and accounting passed'}
(root/'audit.json').write_text(json.dumps(audit,indent=2)+'\n',encoding='utf8');print(json.dumps(audit,indent=2))
