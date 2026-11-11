"""Audit saved sequential forecast and execution artifacts, without refitting."""
import collections,gzip,hashlib,json,math,pathlib,statistics,sys

root=pathlib.Path(sys.argv[1]);pbytes=(root/'predictions.json.gz').read_bytes();tbytes=(root/'trades.json.gz').read_bytes();sbytes=(root/'summary.json').read_bytes()
pred=json.loads(gzip.decompress(pbytes));trades=json.loads(gzip.decompress(tbytes));data=json.loads(sbytes);summary=data['summary'];folds=data['folds']
assert len(folds)==32 and len({(f['symbol'],f['test_month']) for f in folds})==32
for f in folds:assert f['train_start_ms']<f['last_training_target_end_ms']<f['test_start_ms']<f['test_end_ms']
lookup={}
for x in pred:
 key=(x['symbol'],x['model'],x['T']);assert key not in lookup;lookup[key]=x
 assert x['train_start_ms']<x['last_training_target_end_ms']<x['train_end_ms']<=x['T']
 assert x['availability_ms']==x['T']+1000 and x['target_end_ms']==x['T']+61*60000
assert len(pred)==46848
keys=set();closed=0;accounts=collections.defaultdict(list)
for r in trades:
 key=(r['symbol'],r['model'],r['mode'],r['scenario'],r['signal_ms']);assert key not in keys;keys.add(key)
 p=lookup[(r['symbol'],r['model'],r['signal_ms'])];assert r['predicted']==p['predicted']
 assert r['predicted']!=0 and r['direction']==('LONG' if r['predicted']>0 else 'SHORT')
 if r['mode']=='threshold':assert abs(r['predicted'])>=.00375
 if 'net'in r:
  closed+=1;assert r['entry_ms']>r['availability_ms'] and 0<r['exit_ms']-r['entry_ms']<=60*60000
  assert r['estimated_risk']<=5 and r['qty']*max(r['entry'],r['mark_entry'])<=100+1e-8
  assert math.isclose(r['gross']-r['fees']-r['impact']-r['liquidation_fee']+r['funding'],r['net'],abs_tol=1e-9)
  assert math.isclose(r['entry_fee']+r['exit_fee'],r['fees'],abs_tol=1e-9)
  assert math.isclose(sum(x['applied_usdt'] for x in r['funding_events']),r['funding'],abs_tol=1e-9)
  assert all(r['entry_ms']<=x['event_ms']<r['exit_ms'] for x in r['funding_events'])
 if 'entry_ms'in r:accounts[(r['symbol'],r['model'],r['mode'],r['scenario'])].append(r)
for k,rows in accounts.items():
 rows.sort(key=lambda x:x['entry_ms'])
 for a,b in zip(rows,rows[1:]):assert 'exit_ms'in a and a['exit_ms']<=b['entry_ms']
for key,z in summary.items():
 asset,model,mode,scenario=key.split('/')
 rows=[r for r in trades if (r['symbol'],r['model'],r['mode'],r['scenario'])==(asset,model,mode,scenario)]
 complete=[r for r in rows if 'net'in r];open_=[r for r in rows if 'unresolved'in r]
 assert len(complete)==z['closed'] and math.isclose(sum(r['net'] for r in complete),z['net'],abs_tol=1e-8)
 # Only entry fee and funding have hit cash for an unresolved open position.
 expected=1000+z['net']+sum(-r['qty']*r['entry']*.0005+r.get('funding',0) for r in open_)
 assert math.isclose(expected,z['ending_cash'],abs_tol=1e-7)
 assert len(open_)==sum(z['unresolved'].values())
metrics={}
for asset in ('BTCUSDT','ETHUSDT'):
 for model in ('XGBoost','Ridge'):
  rows=[x for x in pred if x['symbol']==asset and x['model']==model];observed=[x for x in rows if x['actual'] is not None];selected=[x for x in observed if abs(x['predicted'])>=.00375]
  avg=lambda xs:statistics.mean(xs) if xs else None
  metrics[f'{asset}/{model}']={'predictions':len(rows),'missing_targets':len(rows)-len(observed),'selected_observed':len(selected),
   'rmse':math.sqrt(statistics.mean((x['predicted']-x['actual'])**2 for x in observed)),
   'selected_mean_prediction':avg([x['predicted'] for x in selected]),'selected_mean_actual':avg([x['actual'] for x in selected]),
   'selected_mean_signed_actual':avg([(1 if x['predicted']>0 else -1)*x['actual'] for x in selected])}
audit={'prediction_rows':len(pred),'selected_records':len(trades),'completed_audited':closed,'folds':len(folds),
 'predictions_sha256':hashlib.sha256(pbytes).hexdigest(),'trades_sha256':hashlib.sha256(tbytes).hexdigest(),'summary_sha256':hashlib.sha256(sbytes).hexdigest(),
 'checks':'unique forecasts and actions, strict historical target cutoff, availability, direction, fees, funding, risk and non-overlap',
 'prediction_metrics':metrics}
(root/'audit.json').write_text(json.dumps(audit,indent=2,sort_keys=True)+'\n',encoding='utf8');print(json.dumps(audit,indent=2))
