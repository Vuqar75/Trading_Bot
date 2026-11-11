"""Calibrate cached original OOF predictions; never refit old forests."""
import datetime as dt,gzip,hashlib,json
from pathlib import Path
import numpy as np
from sklearn.isotonic import IsotonicRegression
from research.pattern_calibration import VERSION,adjust

ROOT=Path(__file__).resolve().parent/'pattern_ai_results'
OUT=Path(__file__).resolve().parent/'pattern_calibration_results'
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')


def month_ms(s):return int(dt.datetime.fromisoformat(s+'-01').replace(tzinfo=dt.timezone.utc).timestamp()*1000)
def shifted(s,n):
    y,m=map(int,s.split('-'));i=y*12+m-1+n;return f'{i//12}-{i%12+1:02d}'


def fit_asset(rows):
    if len(rows)<200:return None
    day=np.asarray([r['T']//86400000 for r in rows]);_,ix,counts=np.unique(day,return_inverse=True,return_counts=True);weights=1/counts[ix]
    p=np.asarray([r['raw_p'] for r in rows]);score=np.asarray([r['raw_net'] for r in rows]);y=np.asarray([r['actual'] for r in rows])
    pi=IsotonicRegression(out_of_bounds='clip').fit(p,(y>0).astype(float),sample_weight=weights)
    ni=IsotonicRegression(out_of_bounds='clip').fit(score,y,sample_weight=weights)
    edges=np.unique(np.quantile(score,np.linspace(0,1,11)))
    if len(edges)<2:return None
    bins=np.searchsorted(edges[1:-1],score,side='right');cells=[]
    for j in range(len(edges)-1):
        mask=bins==j;days=np.unique(day[mask]);daily=np.asarray([y[mask & (day==d)].mean() for d in days])
        lower=float(daily.mean()-2*daily.std(ddof=1)/np.sqrt(len(daily))) if len(daily)>1 else -1.
        cells.append({'examples':int(mask.sum()),'days':len(days),'mean_day_net':float(daily.mean()) if len(days) else None,'lower_score':lower})
    return {'p_x':pi.X_thresholds_.tolist(),'p_y':pi.y_thresholds_.tolist(),'net_x':ni.X_thresholds_.tolist(),'net_y':ni.y_thresholds_.tolist(),
            'edges':edges.tolist(),'cells':cells,'fit_rows':len(rows),'fit_days':len(np.unique(day))}


def calibrator(rows,name,base_id):
    a=month_ms(name);b=month_ms(shifted(name,1));start=month_ms(shifted(name,-3))
    train=[r for r in rows if start<=r['T']<a and r['label_end_ms']<a]
    assets={}
    for s in SYMBOLS:
        c=fit_asset([r for r in train if r['symbol']==s])
        if c is not None:assets[s]=c
    return {'version':VERSION,'base_model_id':base_id,'valid_from_ms':a,'valid_until_ms':b,'max_label_end_ms':max(r['label_end_ms'] for r in train),
            'train_start_ms':start,'assets':assets,'trade_allowed':False}


def metrics(rows):
    if not rows:return {'examples':0}
    y=np.asarray([r['actual'] for r in rows]);raw=np.asarray([r['raw_net'] for r in rows]);cal=np.asarray([r['cal']['expected_stress_net_return'] for r in rows]);binary=(y>0).astype(float)
    rp=np.asarray([r['raw_p'] for r in rows]);cp=np.asarray([r['cal']['p_win_estimate'] for r in rows])
    selected=[r for r in rows if r['cal']['action']=='CANDIDATE']
    return {'examples':len(rows),'raw_net_mse':float(np.mean((raw-y)**2)),'calibrated_net_mse':float(np.mean((cal-y)**2)),
            'raw_brier':float(np.mean((rp-binary)**2)),'calibrated_brier':float(np.mean((cp-binary)**2)),
            'calibrated_candidates':len(selected),'candidate_actual_mean_net_bps':float(np.mean([r['actual'] for r in selected])*10000) if selected else None,
            'candidate_predicted_mean_net_bps':float(np.mean([r['cal']['expected_stress_net_return'] for r in selected])*10000) if selected else None,
            'supported_examples':sum(r['cal']['calibration_supported'] for r in rows)}


def main():
    OUT.mkdir(exist_ok=True)
    with np.load(ROOT/'training_examples.npz') as f:a={k:f[k] for k in ('T','symbol','stress_target','label_end_ms')}
    lookup={(int(t),int(s)):i for i,(t,s) in enumerate(zip(a['T'],a['symbol']))};pred=json.load(gzip.open(ROOT/'predictions.json.gz','rt'));rows=[]
    for p in pred:
        i=lookup[(p['T'],SYMBOLS.index(p['symbol']))]
        if not np.isfinite(a['stress_target'][i]):continue
        rows.append({'T':p['T'],'symbol':p['symbol'],'raw_p':p['p_win'],'raw_net':p['pred_net'],'actual':float(a['stress_target'][i]),'label_end_ms':int(a['label_end_ms'][i])})
    evaluated=[];folds=[]
    for i in range(13):
        name=shifted('2025-10',i);cal=calibrator(rows,name,'historical-'+name);a0=month_ms(name);b=month_ms(shifted(name,1))
        test=[r for r in rows if a0<=r['T']<b];assert cal['max_label_end_ms']<a0
        for r in test:
            result=adjust(r['raw_p'],r['raw_net'],cal,symbol=r['symbol'],signal_ms=r['T'],model_id='historical-'+name)
            if result.get('calibrated'):evaluated.append({**r,'fold':name,'cal':result})
        folds.append({'month':name,'train_start_ms':cal['train_start_ms'],'max_train_label_end_ms':cal['max_label_end_ms'],'test_start_ms':a0,'test_end_ms':b,
                      'asset_training_rows':{s:c['fit_rows'] for s,c in cal['assets'].items()},'test_rows':len(test)})
    total=metrics(evaluated);byasset={s:metrics([r for r in evaluated if r['symbol']==s]) for s in SYMBOLS}
    checks={'net_mse_improves_1pct':total['calibrated_net_mse']<=.99*total['raw_net_mse'],
            'brier_improves_1pct':total['calibrated_brier']<=.99*total['raw_brier'],
            'no_asset_worse_2pct':all(z['calibrated_net_mse']<=1.02*z['raw_net_mse'] and z['calibrated_brier']<=1.02*z['raw_brier'] for z in byasset.values())}
    model=json.loads((ROOT/'model.json').read_text());latest=calibrator(rows,'2026-11',model['model_id']);latest['analysis_promotion_eligible']=all(checks.values());latest['trading_admitted']=False
    latest['calibration_id']=hashlib.sha256(json.dumps(latest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    (OUT/'calibration_candidate.json').write_text(json.dumps(latest,indent=2)+'\n')
    result={'protocol':'PATTERN_CALIBRATION_PROTOCOL.md','total':total,'by_asset':byasset,'checks':checks,'analysis_promotion_eligible':all(checks.values()),
            'trading_admitted':False,'folds':folds,'by_month':{f['month']:metrics([r for r in evaluated if r['fold']==f['month']]) for f in folds},
            'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ('training_examples.npz','predictions.json.gz','model.json')}}
    (OUT/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    with gzip.open(OUT/'predictions.json.gz','wt') as f:json.dump(evaluated,f,separators=(',',':'))
    print(json.dumps({'total':total,'by_asset':byasset,'checks':checks},indent=2))

if __name__=='__main__':main()
