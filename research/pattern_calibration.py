"""Portable, versioned, analysis-only calibration; no sklearn/network/budget."""
import bisect,math
VERSION='pattern-calibration-v1'


def interpolate(x,xs,ys):
    if not xs or len(xs)!=len(ys):raise ValueError('calibration_knots')
    if x<=xs[0]:return ys[0]
    if x>=xs[-1]:return ys[-1]
    j=bisect.bisect_right(xs,x)-1
    return ys[j]+(ys[j+1]-ys[j])*(x-xs[j])/(xs[j+1]-xs[j])


def adjust(raw_p,raw_net,calibrator,*,symbol,signal_ms,model_id=None):
    if calibrator.get('version')!=VERSION:raise ValueError('calibration_version')
    if model_id is not None and calibrator.get('base_model_id')!=model_id:raise ValueError('calibration_model_mismatch')
    if not calibrator['valid_from_ms']<=signal_ms<calibrator['valid_until_ms'] or calibrator['max_label_end_ms']>=signal_ms:raise ValueError('calibration_stale_or_future')
    if not all(type(x) in (int,float) and math.isfinite(x) for x in (raw_p,raw_net)) or not 0<=raw_p<=1:raise ValueError('calibration_input')
    c=calibrator.get('assets',{}).get(symbol)
    if c is None:return {'action':'NO_TRADE','trade_allowed':False,'reason':'calibration_missing_asset','calibrated':False}
    p=interpolate(raw_p,c['p_x'],c['p_y']);net=interpolate(raw_net,c['net_x'],c['net_y'])
    j=bisect.bisect_right(c['edges'][1:-1],raw_net);cell=c['cells'][j]
    inside=c['p_x'][0]<=raw_p<=c['p_x'][-1] and c['net_x'][0]<=raw_net<=c['net_x'][-1]
    support=inside and cell['examples']>=100 and cell['days']>=20
    allow=support and net>=.0005 and cell['lower_score']>0
    return {'action':'CANDIDATE' if allow else 'NO_TRADE','trade_allowed':False,
            'p_win_estimate':float(p),'expected_stress_net_return':float(net),'raw_p_win_estimate':raw_p,'raw_expected_stress_net_return':raw_net,
            'calibrated':True,'calibration_version':VERSION,'calibration_supported':support,'calibration_examples':cell['examples'],
            'calibration_days':cell['days'],'calibration_lower_score':cell['lower_score'],
            'reason':'analysis_only_calibrated_candidate' if allow else 'unsupported_or_nonpositive_calibrated_expectancy'}
