"""Reproduce saved forecasts, purge and real financial fills; never retrain."""
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sys

import joblib
import numpy as np
from threadpoolctl import threadpool_limits

from research import panel_net_ml as p
from research.usdm_intraday_data import zipped
from tools.audit_dynamic_research import audit,same


def main(out,root):
    audit(out/'development.json')
    summary=json.loads((out/'development.json').read_text())
    rows=json.load(gzip.open(out/'dataset.json.gz','rt'))
    dataset_receipt=json.loads((out/'dataset-receipt.json').read_text())
    if hashlib.sha256((out/'dataset.json.gz').read_bytes()).hexdigest()!=dataset_receipt['sha256']:raise ValueError('dataset hash')
    rowmap={(r['T'],r['symbol']):r for r in rows}
    allpred=[];predmap={};forecast_count=0
    for fold in summary['folds']:
        a,b=fold['test_start_ms'],fold['test_end_ms'];train,test,lo=p.split(rows,a,b)
        if max(r['label_end_ms'] for r in train)!=fold['maximum_training_label_end_ms'] or not all(r['label_end_ms']<a for r in train):raise ValueError('purge')
        month=dt.datetime.fromtimestamp(a/1000,dt.timezone.utc).strftime('%Y-%m')
        path=out/(month+'-predictions.json.gz')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=fold['predictions_sha256']:raise ValueError('forecast hash')
        saved=json.load(gzip.open(path,'rt'));allpred.extend(saved)
        Xt=np.array([r['x'] for r in test]);X=np.array([r['x'] for r in train])
        for model in p.MODELS:
            selected=[r for r in saved if r['model']==model]
            if len(selected)!=len(test):raise ValueError('forecast count')
            for side,field in ((-1,'short'),(1,'long')):
                name=month+'-'+model+'-'+str(side)+'.joblib'
                if hashlib.sha256((out/name).read_bytes()).hexdigest()!=fold['models_sha256'][name]:raise ValueError('model hash')
                fitted=joblib.load(out/name)
                if model=='Ridge':
                    valid=np.array([r['labels'][str(side)] is not None for r in train])
                    np.testing.assert_allclose(fitted['standardscaler'].mean_,X[valid].mean(0),rtol=1e-10,atol=1e-10)
                with threadpool_limits(limits=1):actual=fitted.predict(Xt)
                np.testing.assert_allclose(actual,[r[field] for r in selected],rtol=0,atol=0)
                forecast_count+=len(actual)
            for r,original in zip(selected,test):
                if (r['T'],r['symbol'])!=(original['T'],original['symbol']):raise ValueError('forecast association')
                if r['feature_sha256']!=hashlib.sha256(json.dumps(original['x'],separators=(',',':')).encode()).hexdigest():raise ValueError('feature hash')
                predmap[(model,r['T'],r['symbol'])]=r
    if allpred!=json.load(gzip.open(out/'predictions.json.gz','rt')):raise ValueError('joined forecasts changed')
    journals=json.load(gzip.open(out/'development_journal.json.gz','rt'))
    rates={s:{} for s in p.ASSETS}
    for s in p.ASSETS:
        for source in summary['sources'][s]:
            if source['kind']!='fundingRate':continue
            if hashlib.sha256((root/source['file']).read_bytes()).hexdigest()!=source['sha256']:raise ValueError('funding source hash')
            for row in zipped(root/source['file']):
                if row[0].isdigit():rates[s][int(row[0])]=float(row[2])
    matched=0
    for case,journal in journals.items():
        model=case.split('/')[0];result=summary['results'][case];asset_net={s:0. for s in p.ASSETS};months=set();quarters={'2025Q3':0.,'2025Q4':0.}
        for trade in journal['trades']:
            symbol,T,side=trade['symbol'],trade['signal_ms'],trade['sign'];forecast=predmap[(model,T,symbol)]
            row=rowmap[(T,symbol)]
            if row['funding_source_ms']>T-15*p.M:raise ValueError('funding feature lookahead')
            expected_pred=forecast['long' if side==1 else 'short'];same(trade['predicted_stress_net'],expected_pred)
            if expected_pred<.0005 or trade['deadline_ms']!=trade['entry_ms']+240*p.M:raise ValueError('contract')
            if case.endswith('/stress'):
                expected=row['labels'][str(side)]
                if expected is None:raise ValueError('unlabelled execution')
                for field in ('net','gross','fees','impact','funding','qty','entry','exit','entry_ms','exit_ms'):same(trade[field],expected[field])
                if trade['reason']!=expected['reason']:raise ValueError('stress exit label mismatch')
                matched+=1
            actual={e['event_ms']:e['rate'] for e in trade['funding_events']}
            expected={t:r for t,r in rates[symbol].items() if trade['entry_ms']<=t<trade['exit_ms']}
            if actual!=expected:raise ValueError('missed funding event')
            asset_net[symbol]+=trade['net']
            months.add(dt.datetime.fromtimestamp(trade['entry_ms']/1000,dt.timezone.utc).strftime('%Y-%m'))
            quarters['2025Q3' if trade['exit_ms']<p.core.stamp('2025-10-01') else '2025Q4']+=trade['net']
        for s in p.ASSETS:
            same(asset_net[s],result['by_asset'][s]['net'])
            if result['by_asset'][s]['closed']!=sum(t['symbol']==s for t in journal['trades']):raise ValueError('asset count')
        same(result['without_best_asset'],result['net']-max(asset_net.values()))
        if result['active_months']!=len(months):raise ValueError('month coverage')
        for q,n in quarters.items():same(n,result['quarters'][q])
    checks={m:p.criteria(summary['results'][m+'/base'],summary['results'][m+'/stress']) for m in p.MODELS}
    allowed=[m for m in p.MODELS if all(checks[m].values())];allowed.sort(key=lambda m:(-summary['results'][m+'/stress']['net'],summary['results'][m+'/stress']['drawdown'],m))
    if checks!=summary['checks'] or summary['selected']!=(allowed[0] if allowed else None):raise ValueError('candidate policy')
    report={'status':'PASS','forecasts_reproduced':forecast_count,'stress_executions_matched_to_targets':matched,
            'purged_monthly_training':'PASS','training_only_scaler':'PASS','actual_symbol_funding':'PASS',
            'shared_capital':'PASS','selected':summary['selected'],'refits':0,'new_api_calls':0}
    (out/'model_execution_audit.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))
