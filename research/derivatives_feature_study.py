"""One fixed positioning-input experiment on the original model/targets."""
import gzip,hashlib,json
from pathlib import Path
import sys
import numpy as np
from sklearn.ensemble import RandomForestClassifier,RandomForestRegressor
from research.derivative_metrics_data import load,SYMBOLS
from research.pattern_calibration_experiment import month_ms,shifted
from research import technical_patterns as tech

BASE=Path(__file__).resolve().parent/'pattern_ai_results'
NAMES=['oi_log_change_1h','oi_log_change_4h','oi_log_change_24h','top_account_log_ratio','top_position_log_ratio',
       'global_account_log_ratio','taker_log_ratio','position_vs_account_log_ratio','global_ratio_log_change_4h','side_times_global_log_ratio']


def original():
    with np.load(BASE/'training_examples.npz') as f:return {k:f[k] for k in f.files}


def asof(ts,values,queries):
    ix=np.searchsorted(ts,queries,side='right')-1;safe=np.maximum(ix,0)
    valid=(ix>=0)&(queries-ts[safe]<=300000)
    return values[safe],valid,ts[safe]


def prepare(root,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);a=original();data,quality=load(root)
    extra=np.full((len(a['T']),10),np.nan,dtype=np.float32);source_times=np.full((len(a['T']),4),-1,dtype=np.int64)
    for si,symbol in enumerate(SYMBOLS):
        ids=np.flatnonzero((a['symbol']==si)&(a['T']>=month_ms('2025-07')));ts,v=data[symbol];logs=np.log(v);query=a['T'][ids]-900000
        x,ok0,t0=asof(ts,logs,query);h1,ok1,t1=asof(ts,logs,query-3600000);h4,ok4,t4=asof(ts,logs,query-4*3600000);d1,okd,td=asof(ts,logs,query-24*3600000)
        valid=ok0&ok1&ok4&okd
        values=np.column_stack([x[:,0]-h1[:,0],x[:,0]-h4[:,0],x[:,0]-d1[:,0],x[:,1],x[:,2],x[:,3],x[:,4],x[:,2]-x[:,3],x[:,3]-h4[:,3],a['side'][ids]*x[:,3]])
        extra[ids[valid]]=values[valid];source_times[ids[valid]]=np.column_stack([t0,t1,t4,td])[valid]
        quality[symbol]['needed_examples']=len(ids);quality[symbol]['valid_examples']=int(valid.sum());quality[symbol]['coverage']=float(valid.mean())
        assert np.all(t0[valid]<=a['T'][ids[valid]]-900000)
    np.savez_compressed(out/'features.npz',Xextra=extra,source_times=source_times)
    (out/'quality.json').write_text(json.dumps(quality,indent=2)+'\n')
    manifest=Path(root)/'manifest.json';(out/'source_manifest.json').write_bytes(manifest.read_bytes())
    receipt={'features':NAMES,'quality_pass':all(x['coverage']>=.99 for x in quality.values()),
             'features_sha256':hashlib.sha256((out/'features.npz').read_bytes()).hexdigest(),
             'original_data_sha256':hashlib.sha256((BASE/'training_examples.npz').read_bytes()).hexdigest(),
             'metrics_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),'lag_ms':900000}
    (out/'feature_receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(quality,indent=2))
    if not receipt['quality_pass']:raise ValueError('metric_coverage_below_preregistered_99pct')


def metric(rows):
    if not rows:return {'examples':0}
    y=np.asarray([r['actual'] for r in rows]);binary=(y>0).astype(float)
    result={'examples':len(rows)}
    for mode in ('original','extended'):
        net=np.asarray([r[mode+'_net'] for r in rows]);p=np.asarray([r[mode+'_p'] for r in rows]);chosen=[r for r in rows if r[mode+'_p']>=.6 and r[mode+'_net']>=.0005]
        positive=[r for r in rows if r[mode+'_net']>=.0005]
        result[mode]={'net_mse':float(np.mean((net-y)**2)),'brier':float(np.mean((p-binary)**2)),'candidates':len(chosen),
                      'candidate_mean_actual_bps':float(np.mean([r['actual'] for r in chosen])*10000) if chosen else None,
                      'positive_net_forecasts':len(positive),'positive_net_mean_actual_bps':float(np.mean([r['actual'] for r in positive])*10000) if positive else None}
    return result


def evaluate(out):
    out=Path(out);receipt=json.loads((out/'feature_receipt.json').read_text())
    assert receipt['quality_pass'] and hashlib.sha256((out/'features.npz').read_bytes()).hexdigest()==receipt['features_sha256']
    a=original()
    with np.load(out/'features.npz') as f:extra=f['Xextra'];source_times=f['source_times']
    X=np.column_stack([a['X'],extra]);good=np.isfinite(X).all(1)&np.isfinite(a['stress_target'])
    cached=json.load(gzip.open(BASE/'predictions.json.gz','rt'));old={(r['T'],SYMBOLS.index(r['symbol'])):r for r in cached}
    sourcefolds={f['test_month']:f for f in json.loads((BASE/'summary.json').read_text())['folds']}
    results=[];folds=[];params={'n_estimators':64,'max_depth':5,'min_samples_leaf':40,'random_state':42,'n_jobs':2}
    for i in range(10):
        name=shifted('2026-01',i);t0=month_ms(name);t1=month_ms(shifted(name,1));start=month_ms(shifted(name,-6))
        train=np.flatnonzero(good&(a['T']>=start)&(a['T']<t0)&(a['label_end_ms']<t0));test=np.flatnonzero(good&(a['T']>=t0)&(a['T']<t1))
        assert a['label_end_ms'][train].max()<t0
        y=a['stress_target'][train];clf=RandomForestClassifier(**params).fit(X[train],(y>0).astype(int));reg=RandomForestRegressor(**params).fit(X[train],y)
        pp=clf.predict_proba(X[test])[:,1];nn=reg.predict(X[test])
        for j,p,n in zip(test,pp,nn):
            previous=old[(int(a['T'][j]),int(a['symbol'][j]))]
            results.append({'T':int(a['T'][j]),'symbol':SYMBOLS[int(a['symbol'][j])],'fold':name,'actual':float(a['stress_target'][j]),
                            'original_p':previous['p_win'],'original_net':previous['pred_net'],'extended_p':float(p),'extended_net':float(n)})
        folds.append({'month':name,'train_start_ms':start,'test_start_ms':t0,'test_end_ms':t1,'max_train_label_end_ms':int(a['label_end_ms'][train].max()),
                      'extended_train_rows':len(train),'original_train_rows':sourcefolds[name]['train_rows'],'test_rows':len(test),
                      'latest_metric_source_ms':int(source_times[test,0].max())})
        print('positioning_model',name,'train',len(train),'test',len(test),flush=True)
    total=metric(results);byasset={s:metric([r for r in results if r['symbol']==s]) for s in SYMBOLS}
    checks={'net_mse_improves_1pct':total['extended']['net_mse']<=.99*total['original']['net_mse'],
            'brier_improves_1pct':total['extended']['brier']<=.99*total['original']['brier'],
            'no_asset_worse_2pct':all(x['extended']['net_mse']<=1.02*x['original']['net_mse'] and x['extended']['brier']<=1.02*x['original']['brier'] for x in byasset.values())}
    result={'protocol':'PATTERN_DERIVATIVES_PROTOCOL.md','total':total,'by_asset':byasset,'folds':folds,'checks':checks,'analysis_promotion_eligible':all(checks.values()),
            'trading_admitted':False,'features':tech.FEATURES+NAMES,'receipt':receipt,
            'by_month':{f['month']:metric([r for r in results if r['fold']==f['month']]) for f in folds}}
    (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    with gzip.open(out/'predictions.json.gz','wt') as f:json.dump(results,f,separators=(',',':'))
    print(json.dumps({'total':total,'by_asset':byasset,'checks':checks},indent=2))

if __name__=='__main__':
    if sys.argv[1]=='prepare':prepare(Path(sys.argv[2]),Path(sys.argv[3]))
    elif sys.argv[1]=='evaluate':evaluate(Path(sys.argv[2]))
    else:raise SystemExit('prepare/evaluate required')
