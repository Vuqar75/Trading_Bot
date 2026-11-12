"""Offline model/feature diagnosis. No fit, search, provider, VM or budget IO.

Training-space predictions below are numerical probes, not out-of-sample alpha.
--snapshot checks saved packets/history without loading a training framework.
"""
import argparse,calendar,csv,datetime as dt,gzip,hashlib,io,json,sys,zipfile
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research import technical_patterns as tech

ASSETS=('BTCUSDT','ETHUSDT','SOLUSDT')
BAR=900000


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def stamp(s):return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)


def validate_model(bundle):
    original=dict(bundle);identity=original.pop('model_id')
    if hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()!=identity:
        raise ValueError('model_content_identity')
    if bundle['features']!=tech.FEATURES or bundle['feature_version']!=tech.VERSION or bundle['venue']!='binance-usdm' or bundle.get('admitted') is not False:
        raise ValueError('model_contract')
    for kind in ('classifier','regressor'):
        if not bundle[kind]:raise ValueError('empty_forest')
        for tree in bundle[kind]:
            n=len(tree['feature'])
            if not n or any(len(tree[k])!=n for k in ('threshold','left','right','value')):raise ValueError('tree_shape')
            if not np.isfinite(tree['value']).all() or not np.isfinite(tree['threshold']).all():raise ValueError('tree_nonfinite')
            if kind=='classifier' and any(not 0<=v<=1 for v in tree['value']):raise ValueError('classifier_probability')
            for i,f in enumerate(tree['feature']):
                if type(f) is not int:raise ValueError('tree_feature_type')
                if f<0:
                    if tree['left'][i]!=-1 or tree['right'][i]!=-1:raise ValueError('tree_leaf')
                elif f>=len(tech.FEATURES) or not i<tree['left'][i]<n or not i<tree['right'][i]<n:
                    raise ValueError('tree_edge')
    return identity


def native_tree(tree,kind):
    """Independent sklearn C tree walker, restored without any model fitting."""
    from sklearn.tree import _tree
    n=len(tree['feature']);nodes=np.zeros(n,dtype=_tree.NODE_DTYPE)
    for key,source in (('left_child','left'),('right_child','right'),('feature','feature'),('threshold','threshold')):nodes[key]=tree[source]
    nodes['n_node_samples']=1;nodes['weighted_n_node_samples']=1
    values=np.asarray(tree['value'])
    encoded=np.column_stack((1-values,values))[:,None,:] if kind=='classifier' else values[:,None,None]
    restored=_tree.Tree(len(tech.FEATURES),np.array([2 if kind=='classifier' else 1],dtype=np.intp),1)
    restored.__setstate__({'max_depth':5,'node_count':n,'nodes':nodes,'values':encoded})
    return restored


def distribution(value):
    x=np.asarray(value,dtype=float)
    return {'n':len(x),'mean':float(x.mean()),'std':float(x.std()),'quantiles':dict(zip(('min','p01','p10','p50','p90','p99','max'),map(float,np.quantile(x,[0,.01,.1,.5,.9,.99,1]))))}


def forest_audit(bundle,X,y):
    """Verify native parity + bootstrap target means of every saved leaf."""
    if len(bundle['classifier'])!=64 or len(bundle['regressor'])!=64:raise ValueError('original_forest_parameters_changed')
    X=np.ascontiguousarray(X,dtype=np.float32);seeds=np.random.RandomState(42).randint(np.iinfo(np.int32).max,size=64)
    sample=np.unique(np.linspace(0,len(X)-1,min(len(X),2048),dtype=int));result={}
    for kind in ('classifier','regressor'):
        predictions=[];leaf_differences=[];leaves=0;largest=[]
        target=(y>0).astype(float) if kind=='classifier' else y
        for index,tree in enumerate(bundle[kind]):
            native=native_tree(tree,kind);q=native.predict(X)
            predictions.append(q[:,1] if kind=='classifier' else q[:,0]);where=native.apply(X)
            weights=np.bincount(np.random.RandomState(int(seeds[index])).randint(0,len(y),size=len(y)),minlength=len(y))
            leaf_ids=np.flatnonzero(np.asarray(tree['feature'])<0);leaves+=len(leaf_ids)
            largest.append(max(np.mean(where==leaf) for leaf in leaf_ids))
            for leaf in leaf_ids:
                take=where==leaf;den=int(weights[take].sum())
                if not den:raise ValueError('saved_leaf_without_training_support')
                expected=float(np.dot(target[take],weights[take])/den)
                leaf_differences.append(abs(expected-tree['value'][leaf]))
        predicted=np.mean(predictions,axis=0)
        actual=np.asarray([tech.predict_trees(bundle[kind],X[i]) for i in sample])
        parity=float(np.max(np.abs(actual-predicted[sample])))
        difference=max(leaf_differences)
        if parity>1e-12 or difference>1e-12:raise ValueError('export_or_training_target_mismatch')
        result[kind]={'leaves_verified':leaves,'bootstrap_leaf_target_max_abs_error':difference,
                      'portable_native_rows':len(sample),'portable_native_max_abs_error':parity,
                      'prediction_distribution':distribution(predicted),'largest_leaf_mean_row_share':float(np.mean(largest)),
                      'split_counts':{name:sum(t['feature'].count(i) for t in bundle[kind]) for i,name in enumerate(tech.FEATURES)}}
    result['target_distribution']=distribution(y);result['positive_label_fraction']=float(np.mean(y>0))
    result['regressor_prediction_variance_to_label_variance']=result['regressor']['prediction_distribution']['std']**2/float(np.var(y))
    result['scope']='original final-model training population; not a forecast-performance test'
    return result


def packet_audit(bundle,packet):
    """Numerical replay of a recorded input; never synthesize missing features."""
    if not isinstance(packet,dict):return {'status':'invalid_packet'}
    result={'symbol':packet.get('symbol'),'signal_ms':packet.get('signal_ms')}
    ml=packet.get('ml',{});features=packet.get('features',{})
    if not isinstance(ml,dict) or not isinstance(features,dict):return dict(result,status='invalid_packet')
    if ml.get('model_id')!=bundle['model_id']:return dict(result,status='model_id_mismatch')
    if any(k in ml and ml[k]!=packet.get(k) for k in ('symbol','signal_ms')):return dict(result,status='model_identity_mismatch')
    if set(features)!=set(tech.FEATURES[:30]):return dict(result,status='insufficient_feature_evidence',retained_features=len(features))
    if packet.get('feature_version')!=tech.VERSION or packet.get('venue')!='binance-usdm' or packet.get('symbol') not in ASSETS:return dict(result,status='invalid_contract')
    if any(type(features[k]) not in (int,float) for k in tech.FEATURES[:30]):return dict(result,status='invalid_feature_type')
    base=np.asarray([features[k] for k in tech.FEATURES[:30]],dtype=float)
    if not np.isfinite(base).all():return dict(result,status='nonfinite_input')
    detected=packet.get('detected_patterns')
    if not isinstance(detected,list) or any(k not in tech.PATTERNS for k in detected) or len(set(detected))!=len(detected):return dict(result,status='invalid_pattern_schema')
    flags=[name for name in tech.PATTERNS if features[name]==1]
    if any(features[k] not in (0,1) for k in tech.PATTERNS) or set(flags)!=set(detected):
        return dict(result,status='pattern_feature_mismatch')
    signal=packet.get('signal_ms')
    if type(signal) is not int or not bundle['valid_from_ms']<=signal<bundle['valid_until_ms'] or bundle['train_label_end_ms']>=signal:
        return dict(result,status='outside_model_window')
    if ml.get('trade_allowed') is not False:return dict(result,status='invalid_admission')
    expected_side=1 if any(k.endswith('_long') for k in flags) and not any(k.endswith('_short') for k in flags) else -1 if any(k.endswith('_short') for k in flags) and not any(k.endswith('_long') for k in flags) else 0
    if packet.get('side')=='WAIT':
        return dict(result,status='no_direction' if expected_side==0 and ml.get('action')=='NO_TRADE' else 'pattern_direction_mismatch',feature_evidence='full')
    if packet.get('side') not in ('LONG','SHORT'):return dict(result,status='invalid_direction_or_symbol')
    side=1 if packet['side']=='LONG' else -1
    if expected_side!=side:return dict(result,status='pattern_direction_mismatch')
    x=tech.vector(base,side,packet['symbol']).astype(np.float32)
    p=tech.predict_trees(bundle['classifier'],x);net=tech.predict_trees(bundle['regressor'],x)
    values=(ml.get('p_win_estimate'),ml.get('expected_stress_net_return'))
    if any(type(v) not in (int,float) or not np.isfinite(v) for v in values):return dict(result,status='missing_saved_predictions')
    pe=abs(p-values[0]);ne=abs(net-values[1]);action='CANDIDATE' if p>=.6 and net>=.0005 else 'NO_TRADE'
    return dict(result,status='match' if max(pe,ne)<=1e-12 and action==ml.get('action') else 'prediction_mismatch',
                p_win_replayed=p,stress_net_replayed=net,p_max_abs_error=pe,net_max_abs_error=ne,
                p_gate_passes=p>=.6,net_gate_passes=net>=.0005,
                input_sha256=hashlib.sha256(x.tobytes()).hexdigest(),orders_enabled=False)


def compare_features(inputs,trade,volume,origin,start,end,asset):
    """Closed15m reconstruction vs true1m framing and original compact inputs."""
    minute=np.asarray(trade);v=np.asarray(volume)
    reference,reference_flags,_=tech.frame(minute,v,origin)
    a=minute.reshape(-1,15,4)
    aggregate=np.column_stack((a[:,0,0],a[:,:,1].max(1),a[:,:,2].min(1),a[:,-1,3]))
    volumes=v.reshape(-1,15).sum(1)
    live,flags,_=tech.frame(np.repeat(aggregate,15,axis=0),np.repeat(volumes/15,15),origin)
    if not np.array_equal(reference_flags,flags):raise ValueError('aggregation_pattern_mismatch')
    finite=np.isfinite(reference)&np.isfinite(live)
    if not np.array_equal(np.isfinite(reference),np.isfinite(live)):raise ValueError('aggregation_missingness_mismatch')
    agg_error=float(np.max(np.abs(reference[finite]-live[finite])))
    selected=np.flatnonzero((inputs['symbol']==asset)&(inputs['T']>=start)&(inputs['T']<end))
    errors=[];bad_flags=0
    for j in selected:
        i=(int(inputs['T'][j])-origin)//BAR-1
        if i<3000:raise ValueError('comparison_warmup')
        x=tech.vector(live[i],int(inputs['side'][j]),ASSETS[asset]).astype(np.float32)
        errors.append(np.abs(x-inputs['X'][j]));bad_flags+=int(not np.array_equal(flags[i].astype(np.float32),inputs['X'][j,22:30]))
    if not len(errors):raise ValueError('no_matching_feature_cases')
    error=np.max(errors,axis=0)
    if float(error.max())>1e-6 or bad_flags:raise ValueError('training_runtime_feature_mismatch')
    return {'symbol':ASSETS[asset],'origin_ms':origin,'case_start_ms':start,'case_end_ms':end,'matched_cases':len(errors),
            'aggregation_max_abs_error':agg_error,'training_vector_max_abs_error':float(error.max()),'pattern_mismatches':bad_flags,
            'feature_max_abs_error':{k:float(v) for k,v in zip(tech.FEATURES,error) if v>0}}


def archive_features(inputs,archive_root,source_file):
    sources=json.loads(source_file.read_text());reports=[];receipts=[]
    for asset,symbol in enumerate(ASSETS):
        chunks=[]
        for month in range(4,11):
            relative=f'minute/klines/{symbol}-1m-2026-{month:02d}.zip'
            expected=next(r['sha256'] for r in sources[symbol] if r['file']==relative)
            path=archive_root/relative
            if sha(path)!=expected:raise ValueError('pinned_archive_hash_mismatch')
            receipts.append({'file':relative,'sha256':expected})
            with zipfile.ZipFile(path) as archive:
                with archive.open(archive.namelist()[0]) as stream:
                    rows=[(int(r[0]),*map(float,r[1:6])) for r in csv.reader(io.TextIOWrapper(stream)) if r and r[0].isdigit()]
            chunks.append(np.asarray(rows,dtype=float))
        raw=np.concatenate(chunks)
        if not np.isfinite(raw).all() or not np.all(np.diff(raw[:,0])==60000):raise ValueError('archive_minute_gap_or_value')
        if np.any(raw[:,3]<=0) or np.any(raw[:,3]>np.minimum(raw[:,1],raw[:,4])) or np.any(raw[:,2]<np.maximum(raw[:,1],raw[:,4])) or np.any(raw[:,5]<0):raise ValueError('archive_ohlcv_domain')
        for month in range(5,11):
            day=stamp(f'2026-{month:02d}-{calendar.monthrange(2026,month)[1]:02d}')
            origin=day-32*86400000;end=day+86400000;take=(raw[:,0]>=origin)&(raw[:,0]<end);window=raw[take]
            if len(window)!=33*1440 or window[0,0]!=origin or window[-1,0]!=end-60000:raise ValueError('archive_window_incomplete')
            reports.append(compare_features(inputs,window[:,1:5],window[:,5],origin,day,end,asset))
    return {'windows':reports,'archive_receipts':receipts,'matched_cases':sum(r['matched_cases'] for r in reports),
            'max_feature_error':max(r['training_vector_max_abs_error'] for r in reports),'scope':'fixed final UTC day of May..Oct2026,32day warmup,all3assets; no live data'}


def constant_benchmarks(inputs,predictions,folds):
    """Grade cached OOF forecasts against past-only global/asset means; no fit."""
    lookup={(int(t),int(s)):i for i,(t,s) in enumerate(zip(inputs['T'],inputs['symbol']))};rows=[];receipts=[];seen=set()
    for fold in folds:
        train=(inputs['T']>=fold['train_start_ms'])&(inputs['T']<fold['test_start_ms'])&(inputs['label_end_ms']<fold['test_start_ms'])&np.isfinite(inputs['stress_target'])
        if int(train.sum())!=fold['train_rows'] or int(inputs['label_end_ms'][train].max())!=fold['max_train_label_end_ms']:raise ValueError('constant_training_receipt')
        target=inputs['stress_target'];global_net=float(target[train].mean());global_p=float((target[train]>0).mean())
        by_asset={i:(float(target[train&(inputs['symbol']==i)].mean()),float((target[train&(inputs['symbol']==i)]>0).mean())) for i in range(3)}
        cases=[p for p in predictions if p['fold']==fold['test_month']]
        if len(cases)!=fold['test_rows']:raise ValueError('constant_test_row_count')
        for p in cases:
            asset=ASSETS.index(p['symbol']);key=(p['T'],asset)
            if key in seen or not fold['test_start_ms']<=p['T']<fold['test_end_ms']:raise ValueError('constant_test_identity')
            seen.add(key);i=lookup[key];y=float(target[i])
            if not np.isfinite(y):raise ValueError('constant_missing_target')
            asset_net,asset_p=by_asset[asset]
            rows.append({'month':fold['test_month'],'asset':p['symbol'],'y':y,'rf_net':p['pred_net'],'rf_p':p['p_win'],
                         'global_net':global_net,'global_p':global_p,'asset_net':asset_net,'asset_p':asset_p})
        receipts.append({k:fold[k] for k in ('test_month','train_start_ms','test_start_ms','test_end_ms','max_train_label_end_ms','train_rows','test_rows')})
    if len(rows)!=len(predictions):raise ValueError('constant_unmatched_predictions')
    def metrics(selected):
        y=np.array([r['y'] for r in selected]);binary=(y>0).astype(float);result={'n':len(y)}
        for model in ('rf','global','asset'):
            net=np.array([r[model+'_net'] for r in selected]);p=np.array([r[model+'_p'] for r in selected])
            result[model]={'net_mse':float(np.mean((net-y)**2)),'brier':float(np.mean((p-binary)**2))}
        return result
    return {'scope':'same cached purged OOF forecasts; overlapping labels,not independent confirmation',
            'total':metrics(rows),'by_asset':{s:metrics([r for r in rows if r['asset']==s]) for s in ASSETS},
            'by_month':{f['test_month']:metrics([r for r in rows if r['month']==f['test_month']]) for f in folds},'fold_receipts':receipts}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',type=Path,default=ROOT/'research/pattern_ai_results/model.json')
    parser.add_argument('--inputs',type=Path,default=ROOT/'research/pattern_ai_results/training_examples.npz')
    parser.add_argument('--archive-root',type=Path);parser.add_argument('--snapshot',type=Path,action='append',default=[])
    parser.add_argument('--snapshot-only',action='store_true');parser.add_argument('--output',type=Path)
    args=parser.parse_args();bundle=json.loads(args.model.read_text());identity=validate_model(bundle)
    report={'model_id':identity,'model_sha256':sha(args.model),'model_changed':False,'training_performed':False,'network_used':False,'orders_enabled':False,'snapshots':[]}
    if not args.snapshot_only:
        import sklearn
        report['libraries']={'numpy':np.__version__,'sklearn':sklearn.__version__}
        with np.load(args.inputs,allow_pickle=False) as f:inputs={k:f[k] for k in f.files}
        n=len(inputs['T'])
        if inputs['X'].shape!=(n,len(tech.FEATURES)) or len(set(zip(inputs['T'].tolist(),inputs['symbol'].tolist())))!=n or not np.isfinite(inputs['X']).all():raise ValueError('feature_input_quality')
        if not np.all(inputs['T']%BAR==0) or not np.all(inputs['label_end_ms']>inputs['T']):raise ValueError('input_time_grain')
        for asset in range(3):
            if not np.array_equal(inputs['X'][:,31+asset],(inputs['symbol']==asset).astype(np.float32)):raise ValueError('input_asset_binding')
        if not np.array_equal(inputs['X'][:,30],inputs['side']):raise ValueError('input_side_binding')
        mask=(inputs['T']>=stamp('2026-05-01'))&(inputs['T']<bundle['valid_from_ms'])&(inputs['label_end_ms']<bundle['valid_from_ms'])&np.isfinite(inputs['stress_target'])
        if int(inputs['label_end_ms'][mask].max())!=bundle['train_label_end_ms']:raise ValueError('training_cutoff_mismatch')
        report['inputs']={'rows':n,'final_training_rows':int(mask.sum()),'sha256':sha(args.inputs),'duplicate_keys':0,'nonfinite_features':0}
        report['forest']=forest_audit(bundle,inputs['X'][mask],inputs['stress_target'][mask])
        summary=args.inputs.parent/'summary.json';predictions=args.inputs.parent/'predictions.json.gz'
        report['constant_benchmarks']=constant_benchmarks(inputs,json.load(gzip.open(predictions,'rt')),json.loads(summary.read_text())['folds'])
        report['benchmark_source_hashes']={p.name:sha(p) for p in (summary,predictions)}
        report['training_by_asset']={asset:{'rows':int(np.sum(mask&(inputs['symbol']==i))),'positive_fraction':float(np.mean(inputs['stress_target'][mask&(inputs['symbol']==i)]>0)),
                                                'mean_net':float(np.mean(inputs['stress_target'][mask&(inputs['symbol']==i)]))} for i,asset in enumerate(ASSETS)}
        if args.archive_root:report['feature_replay']=archive_features(inputs,args.archive_root,ROOT/'research/pattern_ai_results/sources.json')
    for path in args.snapshot:
        value=json.loads(path.read_text())
        packets=[r['packet'] for r in value['records']] if value.get('version')=='technical-plan-history-v1' else [value]
        report['snapshots'].append({'file':path.name,'sha256':sha(path),'checks':[packet_audit(bundle,p) for p in packets]})
    raw=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:args.output.write_text(raw)
    else:print(raw,end='')


if __name__=='__main__':main()
