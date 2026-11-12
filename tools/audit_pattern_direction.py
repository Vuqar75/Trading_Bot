"""Independent cached-case/metric receipt, no training or runtime access."""
import gzip,hashlib,json,sys
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]


def main():
    source=ROOT/'research/pattern_ai_results';out=ROOT/'research/pattern_direction_results'
    summary=json.loads((out/'summary.json').read_text());receipt=summary['source_receipt']
    for name,expected in receipt['sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise ValueError('source_changed')
    prediction_file=out/'predictions.npz'
    if hashlib.sha256(prediction_file.read_bytes()).hexdigest()!=summary['predictions_sha256']:raise ValueError('prediction_hash')
    with np.load(prediction_file,allow_pickle=False) as f:pred={k:f[k] for k in f.files}
    with np.load(source/'training_examples.npz',allow_pickle=False) as f:data={k:f[k] for k in ('T','symbol','side','label_end_ms','stress_target')}
    reference=json.load(gzip.open(source/'predictions.json.gz','rt'));assets=('BTCUSDT','ETHUSDT','SOLUSDT')
    indices=pred['source_index'];p=pred['p_win'];net=pred['pred_net'];kept=pred['nonoverlap_source_index']
    if not len(indices)==len(p)==len(net)==len(reference)==summary['paired_cases'] or len(np.unique(indices))!=len(indices):raise ValueError('paired_count')
    if not np.isfinite(p).all() or not np.isfinite(net).all() or not np.all((0<=p)&(p<=1)):raise ValueError('prediction_domain')
    keys=[(int(data['T'][i]),assets[int(data['symbol'][i])]) for i in indices]
    if keys!=[(r['T'],r['symbol']) for r in reference]:raise ValueError('pair_order_or_identity')
    y=data['stress_target'][indices]
    mse=float(np.mean((net-y)**2));brier=float(np.mean((p-(y>0))**2))
    for actual,field in ((mse,'net_mse'),(brier,'brier')):
        if not np.isclose(actual,summary['candidate_metrics']['total']['rf'][field],rtol=1e-12,atol=1e-15):raise ValueError('metric_mismatch')
    for fold in summary['folds']:
        start=fold['test_start_ms'];end=fold['test_end_ms']
        selected=(data['T'][indices]>=start)&(data['T'][indices]<end)
        if int(selected.sum())!=fold['test_rows'] or not fold['max_train_label_end_ms']<start:raise ValueError('fold_receipt')
        train=(data['T']>=fold['train_start_ms'])&(data['T']<start)&(data['label_end_ms']<start)&np.isfinite(data['stress_target'])
        if int(train.sum())!=fold['train_rows']:raise ValueError('purged_training_count')
    gate_indices=indices[(p>=.6)&(net>=.0005)]
    if len(gate_indices)!=summary['economic_screen']['gate_cases_before_overlap_filter'] or not set(kept)<=set(gate_indices):raise ValueError('candidate_gate')
    for asset in range(3):
        one=kept[data['symbol'][kept]==asset]
        if any(data['T'][b]<data['label_end_ms'][a] for a,b in zip(one,one[1:])):raise ValueError('overlapping_cases')
    if summary['trading_admitted'] is not False or summary['model_exported'] is not False or summary['economic_screen']['portfolio_pnl'] is not None or summary['economic_screen']['executed_trades'] is not False:raise ValueError('false_admission_or_pnl')
    result={'paired_rows':len(indices),'folds_verified':len(summary['folds']),'source_hashes':'match','predictions_hash':'match',
            'independent_net_mse':mse,'independent_brier':brier,'gate_cases':len(gate_indices),'nonoverlap_cases':len(kept),
            'training_performed_by_audit':False,'new_market_or_provider_requests':False,'status':'PASS'}
    (out/'audit.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__':main()
