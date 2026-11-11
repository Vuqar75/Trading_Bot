"""Save compact auditable ML inputs and verify actual ledgers; no model training."""
import datetime as dt,gzip,hashlib,json,math
from pathlib import Path
import numpy as np
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research import technical_patterns as tech
from trading_bot.technical_ai_view import render
ROOT=Path(__file__).resolve().parents[1];P=ROOT/'research/pattern_ai_results'
raw=gzip.open(P/'examples.json.gz','rb').read();rows=json.loads(raw)
symbols=('BTCUSDT','ETHUSDT','SOLUSDT')
arr={'T':np.asarray([r['T'] for r in rows],dtype=np.int64),'label_end_ms':np.asarray([r['label_end_ms'] for r in rows],dtype=np.int64),
     'symbol':np.asarray([symbols.index(r['symbol']) for r in rows],dtype=np.int8),'side':np.asarray([r['side'] for r in rows],dtype=np.int8),
     'X':np.asarray([r['x'] for r in rows],dtype=np.float32),
     'stress_target':np.asarray([r['stress'].get('net_return',np.nan) for r in rows],dtype=np.float64),
     'base_net':np.asarray([r['base'].get('net',np.nan) for r in rows],dtype=np.float64),
     'stress_net':np.asarray([r['stress'].get('net',np.nan) for r in rows],dtype=np.float64)}
np.savez_compressed(P/'training_examples.npz',**arr)
summary=json.loads((P/'summary.json').read_text());preds=json.load(gzip.open(P/'predictions.json.gz','rt'));journals=json.load(gzip.open(P/'trades.json.gz','rt'))
for f in summary['folds']:
    assert f['max_train_label_end_ms']<f['test_start_ms']
    mask=(arr['T']>=f['train_start_ms'])&(arr['T']<f['test_start_ms'])&(arr['label_end_ms']<f['test_start_ms'])&np.isfinite(arr['stress_target'])
    assert int(mask.sum())==f['train_rows']
folds={f['test_month']:f for f in summary['folds']}
for p in preds:
    f=folds[p['fold']];assert f['test_start_ms']<=p['T']<f['test_end_ms']
    assert 0<=p['p_win']<=1 and math.isfinite(p['pred_net'])
audit={}
for key,trades in journals.items():
    stress=key.endswith('/stress');impact=.001 if stress else .0005;previous=0
    for r in trades:
        def eq(a,b):assert math.isclose(a,b,abs_tol=1e-8),(key,a,b)
        eq(r['gross'],r['qty']*r['side']*(r['exit_reference']-r['entry_reference']))
        eq(r['net'],r['gross']-r['fees']-r['impact']+r['funding']-r['liquidation_fee'])
        eq(r['fees'],r['qty']*.0005*(r['entry']+r['exit']))
        eq(r['funding'],sum(f['applied'] for f in r['funding_events']))
        eq(r['entry'],r['entry_reference']*(1+r['side']*impact));eq(r['exit'],r['exit_reference']*(1-r['side']*impact))
        assert r['entry_ms']-r['signal_ms']==(6 if stress else 1)*60000
        assert previous<=r['entry_ms']<r['exit_ms'];previous=r['exit_ms']
        assert r['estimated_risk']<=5 and r['hold_min']<=240
    assert abs(sum(x['net'] for x in trades)-summary['results'][key]['net'])<1e-7
    audit[key]={'closed_records':len(trades),'ledger_time_and_limits':'PASS'}
p=np.asarray([r['p_win'] for r in preds]);n=np.asarray([r['pred_net'] for r in preds])
audit['selection_diagnosis']={'predictions':len(preds),'max_p_win':float(p.max()),'max_predicted_stress_net':float(n.max()),
                            'above_p_gate':int((p>=.6).sum()),'above_net_gate':int((n>=.0005).sum()),'above_both':int(((p>=.6)&(n>=.0005)).sum()),
                            'conclusion':'No trading system qualified: conservative probability gate admits no forecasts. Do not interpret no-trade as profitable alpha.'}
audit['input_receipt']={'counterfactual_rows':len(rows),'uncompressed_examples_sha256':hashlib.sha256(raw).hexdigest(),'compressed_local_bytes':(P/'examples.json.gz').stat().st_size,
                        'committed_npz_bytes':(P/'training_examples.npz').stat().st_size,'features':tech.FEATURES,'symbols':symbols,
                        'note':'Full counterfactual JSON retained locally and reproducible from pinned public archives; compact exact ML inputs committed instead of33MBduplicate.'}
(P/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
# Demonstrate the last archived BTC forecast, irrespective of PnL, using saved
# causal October model predictions; no refit, no use of its future outcome.
pred=max((p for p in preds if p['symbol']=='BTCUSDT'),key=lambda p:p['T'])
row=next(r for r in reversed(rows) if r['T']==pred['T'] and r['symbol']=='BTCUSDT')
packet={'schema':1,'feature_version':tech.VERSION,'symbol':'BTCUSDT','venue':'binance-usdm','as_of_ms':row['T']+1000,'signal_ms':row['T'],'expires_ms':row['T']+900000,
        'side':'LONG' if row['side']==1 else 'SHORT','historical_example':True,'detected_patterns':row['patterns'],'features':dict(zip(tech.FEATURES[:30],row['x'][:30])),
        'ml':{'action':'CANDIDATE' if pred['p_win']>=.6 and pred['pred_net']>=.0005 else 'NO_TRADE','trade_allowed':False,
              'p_win_estimate':pred['p_win'],'expected_stress_net_return':pred['pred_net'],'model_id':'causal-fold-'+pred['fold']},
        'fold_receipt':folds[pred['fold']]}
(P/'pattern_example.json').write_text(json.dumps(packet,indent=2,ensure_ascii=False)+'\n')
(P/'pattern_example.txt').write_text('[Исторический пример, не текущий рынок]\n'+render(P/'pattern_example.json',now=(row['T']+1000)/1000)+'\n')
(P/'.gitignore').write_text('/examples.json.gz\n')
print(json.dumps({'rows':len(rows),'predictions':len(preds),'audited_trades':sum(len(v) for v in journals.values()),'npz_bytes':(P/'training_examples.npz').stat().st_size,'selection':audit['selection_diagnosis']},indent=2))
