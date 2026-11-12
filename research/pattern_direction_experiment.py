"""One frozen representation experiment over cached cases; no runtime changes."""
import argparse,gzip,hashlib,json,time
from pathlib import Path
import numpy as np
from sklearn.ensemble import RandomForestClassifier,RandomForestRegressor
from .pattern_direction_features import transform,FEATURES,VERSION
from .technical_patterns import tree_json,predict_trees
from tools.audit_pattern_runtime import constant_benchmarks,ASSETS

ROOT=Path(__file__).resolve().parents[1]
INPUT=ROOT/'research/pattern_ai_results'
OUTPUT=ROOT/'research/pattern_direction_results'
PARAMS={'n_estimators':64,'max_depth':5,'min_samples_leaf':40,'random_state':42,'n_jobs':2,'bootstrap':True}


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def nonoverlap_cases(inputs,p,net,indices):
    chosen=indices[(p>=.60)&(net>=.0005)];kept=[]
    for asset in range(3):
        busy=-1
        for i in sorted(chosen[inputs['symbol'][chosen]==asset],key=lambda j:(inputs['T'][j],j)):
            if inputs['T'][i]<busy:continue
            kept.append(int(i));busy=int(inputs['label_end_ms'][i])
    kept=np.array(sorted(kept,key=lambda j:(inputs['T'][j],inputs['symbol'][j])),dtype=np.int64)
    y=inputs['stress_target'][kept]
    by_asset={s:{'cases':int(np.sum(inputs['symbol'][kept]==i)),
                 'mean_stress_target':float(y[inputs['symbol'][kept]==i].mean()) if np.any(inputs['symbol'][kept]==i) else None} for i,s in enumerate(ASSETS)}
    days=len(np.unique(inputs['T'][kept]//86400000));directions={str(s):int(np.sum(inputs['side'][kept]==s)) for s in (-1,1)}
    mean=float(y.mean()) if len(y) else None
    without_best=float((y.sum()-y.max())/(len(y)-1)) if len(y)>1 else None
    checks={'at_least_60_nonoverlap_cases':len(kept)>=60,'at_least_30_days':days>=30,
            'at_least_10_per_asset':all(v['cases']>=10 for v in by_asset.values()),
            'at_least_10_each_direction':all(n>=10 for n in directions.values()),
            'positive_mean_stress':mean is not None and mean>0,
            'positive_each_asset':all(v['mean_stress_target'] is not None and v['mean_stress_target']>0 for v in by_asset.values()),
            'positive_without_best':without_best is not None and without_best>0}
    return {'gate_cases_before_overlap_filter':len(chosen),'nonoverlap_cases':len(kept),'utc_days':days,
            'by_asset':by_asset,'by_side':directions,'mean_stress_target':mean,'mean_without_best':without_best,
            'checks':checks,'screen_passed':all(checks.values()),'portfolio_pnl':None,'executed_trades':False},kept


def quality(candidate,reference):
    total={key:min(reference['total'][m][key] for m in ('rf','global','asset')) for key in ('net_mse','brier')}
    checks={key+'_improves_best_1pct':candidate['total']['rf'][key]<=.99*total[key] for key in total}
    checks['no_asset_worse_2pct']=all(candidate['by_asset'][s]['rf'][key]<=1.02*min(reference['by_asset'][s][m][key] for m in ('rf','global','asset')) for s in ASSETS for key in total)
    months=sum(candidate['by_month'][name]['rf']['net_mse']<min(values[m]['net_mse'] for m in ('rf','global','asset')) for name,values in reference['by_month'].items())
    checks['at_least_9_months_net_mse_better']=months>=9
    return {'best_reference':total,'net_mse_better_months':months,'checks':checks,'screen_passed':all(checks.values())}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=OUTPUT);args=parser.parse_args()
    receipt=json.loads((ROOT/'PATTERN_DIRECTION_INPUTS.json').read_text())
    for name,digest in receipt['sha256'].items():
        if sha(ROOT/name)!=digest:raise ValueError('frozen_source_changed:'+name)
    with np.load(INPUT/'training_examples.npz',allow_pickle=False) as archive:inputs={k:archive[k] for k in archive.files}
    if len(set(zip(inputs['T'].tolist(),inputs['symbol'].tolist())))!=len(inputs['T']):raise ValueError('duplicate_cases')
    if not np.array_equal(inputs['X'][:,30],inputs['side']) or not np.all(inputs['T']%900000==0):raise ValueError('source_side_or_time')
    for asset in range(3):
        if not np.array_equal(inputs['X'][:,31+asset],(inputs['symbol']==asset).astype(np.float32)):raise ValueError('source_asset')
    X=transform(inputs['X']);target=inputs['stress_target']
    reference_rows=json.load(gzip.open(INPUT/'predictions.json.gz','rt'));folds=json.loads((INPUT/'summary.json').read_text())['folds']
    lookup={(int(t),int(s)):i for i,(t,s) in enumerate(zip(inputs['T'],inputs['symbol']))};predictions=[];all_indices=[];fold_records=[];elapsed=time.monotonic()
    for number,fold in enumerate(folds):
        train=(inputs['T']>=fold['train_start_ms'])&(inputs['T']<fold['test_start_ms'])&(inputs['label_end_ms']<fold['test_start_ms'])&np.isfinite(target)
        if int(train.sum())!=fold['train_rows'] or int(inputs['label_end_ms'][train].max())!=fold['max_train_label_end_ms']:raise ValueError('training_receipt_mismatch')
        rows=[r for r in reference_rows if r['fold']==fold['test_month']]
        indices=np.array([lookup[(r['T'],ASSETS.index(r['symbol']))] for r in rows],dtype=np.int64)
        if len(indices)!=fold['test_rows'] or not np.isfinite(target[indices]).all():raise ValueError('test_pairing_mismatch')
        if not np.all((inputs['T'][indices]>=fold['test_start_ms'])&(inputs['T'][indices]<fold['test_end_ms'])):raise ValueError('test_window_mismatch')
        classifier=RandomForestClassifier(**PARAMS,max_features='sqrt',class_weight=None,criterion='gini')
        regressor=RandomForestRegressor(**PARAMS,max_features=1.,criterion='squared_error')
        classifier.fit(X[train],(target[train]>0).astype(int));regressor.fit(X[train],target[train])
        p=classifier.predict_proba(X[indices])[:,list(classifier.classes_).index(1)];net=regressor.predict(X[indices])
        ct=tree_json(classifier,True);rt=tree_json(regressor)
        sample=np.unique(np.linspace(0,len(indices)-1,min(10,len(indices)),dtype=int));parity=0.
        for j in sample:
            parity=max(parity,abs(predict_trees(ct,X[indices[j]])-p[j]),abs(predict_trees(rt,X[indices[j]])-net[j]))
        if parity>1e-9:raise ValueError('portable_parity_failure')
        predictions.extend({'T':r['T'],'symbol':r['symbol'],'fold':r['fold'],'p_win':float(pp),'pred_net':float(nn)} for r,pp,nn in zip(rows,p,net))
        all_indices.extend(indices.tolist())
        fold_records.append({**fold,'feature_version':VERSION,'portable_max_error':float(parity)})
        print(json.dumps({'fold':fold['test_month'],'train':int(train.sum()),'test':len(indices),'max_p':float(p.max()),'gate_cases':int(np.sum((p>=.6)&(net>=.0005))),'elapsed_seconds':round(time.monotonic()-elapsed,2)}),flush=True)
    if len(predictions)!=len(reference_rows) or len(set(all_indices))!=len(all_indices):raise ValueError('paired_case_coverage')
    candidate=constant_benchmarks(inputs,predictions,folds);reference=constant_benchmarks(inputs,reference_rows,folds)
    p=np.array([r['p_win'] for r in predictions]);net=np.array([r['pred_net'] for r in predictions]);indices=np.array(all_indices,dtype=np.int64)
    economic,kept=nonoverlap_cases(inputs,p,net,indices);forecast=quality(candidate,reference)
    result={'protocol':'PATTERN_DIRECTION_PROTOCOL.md','feature_version':VERSION,'features':FEATURES,'params':PARAMS,
            'paired_cases':len(indices),'candidate_metrics':candidate,'reference_metrics':reference,'forecast_screen':forecast,'economic_screen':economic,
            'further_execution_validation_warranted':forecast['screen_passed'] and economic['screen_passed'],'trading_admitted':False,'model_exported':False,
            'max_probability':float(p.max()),'max_predicted_net':float(net.max()),'folds':fold_records,'source_receipt':receipt,
            'libraries':{'numpy':np.__version__},'elapsed_seconds':round(time.monotonic()-elapsed,3)}
    import sklearn
    result['libraries']['sklearn']=sklearn.__version__
    args.output.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(args.output/'predictions.npz',source_index=indices,p_win=p,pred_net=net,nonoverlap_source_index=kept)
    result['predictions_sha256']=sha(args.output/'predictions.npz')
    (args.output/'summary.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'forecast':forecast,'economic':economic,'further_validation':result['further_execution_validation_warranted']},allow_nan=False),flush=True)


if __name__=='__main__':main()
