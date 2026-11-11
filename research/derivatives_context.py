"""Read-only public derivatives FACTS; never a learned entry signal."""
import bisect,math
from concurrent.futures import ThreadPoolExecutor

VERSION='derivatives-context-v1';STEP=300000;LAG=900000
SPECS={
 'oi':('/futures/data/openInterestHist','sumOpenInterest',500,False),
 'top_accounts':('/futures/data/topLongShortAccountRatio','longShortRatio',12,False),
 'top_positions':('/futures/data/topLongShortPositionRatio','longShortRatio',12,False),
 'global_accounts':('/futures/data/globalLongShortAccountRatio','longShortRatio',100,False),
 'taker':('/futures/data/takerlongshortRatio','buySellRatio',12,True)}
PATHS=frozenset(s[0] for s in SPECS.values())


def series(rows,field,symbol,now_ms,start_labeled=False):
    if not isinstance(rows,list) or len(rows)>500:raise ValueError('metric_response_shape')
    values={}
    for row in rows:
        if not isinstance(row,dict) or ('symbol' not in row and not start_labeled) or row.get('symbol',symbol)!=symbol:raise ValueError('metric_symbol')
        t=row.get('timestamp')
        if type(t) is not int or t%STEP:raise ValueError('metric_timestamp')
        end=t+STEP if start_labeled else t
        value=float(row[field])
        if not math.isfinite(value) or value<=0:raise ValueError('metric_value')
        if end>now_ms:continue
        if end in values and values[end]!=value:raise ValueError('metric_conflict')
        values[end]=value
    return sorted(values.items())


def at(rows,cut):
    i=bisect.bisect_right([r[0] for r in rows],cut)-1
    if i<0 or cut-rows[i][0]>STEP:raise ValueError('metric_missing_or_stale')
    return rows[i]


def collect(get,*,symbol,signal_ms,now_ms,clock=None):
    if symbol not in ('BTCUSDT','ETHUSDT','SOLUSDT') or type(signal_ms) is not int or signal_ms%900000 or not signal_ms<=now_ms<signal_ms+900000:raise ValueError('metric_context_time')
    cutoff=signal_ms-LAG
    def one(item):
        name,(path,field,limit,start)=item
        try:
            raw=get(path,{'symbol':symbol,'period':'5m','limit':limit,'endTime':now_ms})
            data=series(raw,field,symbol,now_ms,start);last,value=at(data,cutoff)
            return name,{'timestamp_ms':last,'value':value},data,None
        except (ValueError,TypeError,KeyError,OSError) as exc:
            reason=str(exc) if str(exc).startswith('public_market_unavailable_http') else 'metric_unavailable_or_invalid'
            return name,None,[],reason
    with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(one,SPECS.items()))
    facts={};times={};missing={};history={}
    mapping={'top_accounts':'top_account_long_short_ratio','top_positions':'top_position_long_short_ratio','global_accounts':'global_account_long_short_ratio','taker':'taker_buy_sell_ratio'}
    for name,item,data,error in results:
        if error:missing[name]=error;continue
        history[name]=data;times[name]=item['timestamp_ms']
        if name=='oi':facts['open_interest_units']=item['value']
        else:facts[mapping[name]]=item['value']
    if 'oi' in history:
        oi=facts['open_interest_units']
        for hours in (1,4,24):
            try:
                old_t,old=at(history['oi'],cutoff-hours*3600000)
                facts[f'oi_change_{hours}h_pct']=(oi/old-1)*100;times[f'oi_{hours}h']=old_t
            except ValueError:missing[f'oi_change_{hours}h']='metric_missing_or_stale'
    observed=now_ms if clock is None else clock()
    result={'version':VERSION,'symbol':symbol,'venue':'binance-usdm','signal_ms':signal_ms,'cutoff_ms':cutoff,
            'requested_ms':now_ms,'observed_ms':observed,'status':'available' if not missing else 'partial' if facts else 'unavailable','facts':facts,'source_times_ms':times,'missing':missing,
            'source_period_ms':STEP,'conservative_lag_ms':LAG,'used_by_local_ml':False,
            'semantics':'OI measures total open interest,not direction.Account and top-position ratios describe different populations.Taker ratio is flow,not executable liquidity or proof of liquidation.'}
    return validate(result,symbol=symbol,signal_ms=signal_ms,as_of_ms=observed)


def validate(context,*,symbol,signal_ms,as_of_ms):
    if context.get('version')!=VERSION or context.get('venue')!='binance-usdm' or context.get('symbol')!=symbol:raise ValueError('derivatives_market_schema')
    cutoff=signal_ms-LAG
    if context.get('signal_ms')!=signal_ms or context.get('cutoff_ms')!=cutoff or context.get('source_period_ms')!=STEP or context.get('conservative_lag_ms')!=LAG or context.get('used_by_local_ml') is not False:raise ValueError('derivatives_binding')
    requested=context.get('requested_ms');observed=context.get('observed_ms')
    if type(requested) is not int or type(observed) is not int or not signal_ms<=requested<=observed<=as_of_ms or observed-requested>30000:raise ValueError('derivatives_observation_time')
    facts=context.get('facts');times=context.get('source_times_ms');missing=context.get('missing');status=context.get('status')
    if not isinstance(facts,dict) or not isinstance(times,dict) or not isinstance(missing,dict):raise ValueError('derivatives_values')
    required={'open_interest_units','top_account_long_short_ratio','top_position_long_short_ratio','global_account_long_short_ratio','taker_buy_sell_ratio','oi_change_1h_pct','oi_change_4h_pct','oi_change_24h_pct'}
    if set(facts)-required or any(type(v) not in (int,float) or not math.isfinite(v) or (v<=-100 if k.startswith('oi_change_') else v<=0) for k,v in facts.items()):raise ValueError('derivatives_numeric_value')
    if (status=='available' and (set(facts)!=required or missing)) or (status=='partial' and (not facts or not missing)) or (status=='unavailable' and (facts or not missing)) or status not in ('available','partial','unavailable'):raise ValueError('derivatives_status')
    if any(not isinstance(v,str) or len(v)>100 for v in missing.values()):raise ValueError('derivatives_diagnostic')
    offsets={'oi':0,'top_accounts':0,'top_positions':0,'global_accounts':0,'taker':0,'oi_1h':3600000,'oi_4h':4*3600000,'oi_24h':24*3600000}
    if set(times)-set(offsets):raise ValueError('derivatives_source_key')
    for k,t in times.items():
        if type(t) is not int or not 0<=cutoff-offsets[k]-t<=STEP:raise ValueError('derivatives_source_time')
    required_sources={'open_interest_units':('oi',),'top_account_long_short_ratio':('top_accounts',),'top_position_long_short_ratio':('top_positions',),'global_account_long_short_ratio':('global_accounts',),'taker_buy_sell_ratio':('taker',),'oi_change_1h_pct':('oi','oi_1h'),'oi_change_4h_pct':('oi','oi_4h'),'oi_change_24h_pct':('oi','oi_24h')}
    if any(any(k not in times for k in required_sources[f]) for f in facts):raise ValueError('derivatives_fact_source_missing')
    return context
