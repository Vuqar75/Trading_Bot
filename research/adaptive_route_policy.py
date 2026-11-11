"""Fixed research routing decisions; no execution, network, or API access."""
import hashlib,json,math
from .adaptive_shadow import validate_model
from .adaptive_study import signals

VERSION='isolated-router-policy-v1'
FUTURES_VERSION='isolated-futures-two-sided-v2'
MAPPING={'TREND_UP':('spot','trend1','LONG'),'TREND_DOWN':('futures','trend1','SHORT'),
         'RANGE':('spot','range','LONG')}
CAPABILITIES={'spot':('TREND_UP','RANGE'),'futures':('TREND_DOWN',),
              'futures_cross':(),'margin_cross':()}

def decision_digest(decision):
    return hashlib.sha256(json.dumps(decision,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def candle_digest(bars,skip_bar):
    return hashlib.sha256(json.dumps({'bars':bars,'skip_bar':skip_bar},sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def route(decision,*,snapshot_id,data_ms,now_ms,symbol,decision_version,market=None,eligible=False,research_sandbox=False,policy=VERSION):
    """Return admission metadata; rejected strategies require isolated sandbox."""
    if not research_sandbox:return {'allowed':False,'reason':'research_sandbox_required'}
    if policy not in (VERSION,FUTURES_VERSION):return {'allowed':False,'reason':'unsupported_route_policy'}
    if symbol!='BTCUSDT':return {'allowed':False,'reason':'unsupported_symbol'}
    if decision_version!='regular-regime-horizon-v2':return {'allowed':False,'reason':'unsupported_decision_version'}
    try:
        if type(now_ms) is not int or type(data_ms) is not int:raise ValueError('invalid_time')
        validate_model(decision,snapshot_id,data_ms,now_ms)
    except (ValueError,TypeError,KeyError):return {'allowed':False,'reason':'invalid_or_expired_decision'}
    if decision['mode']=='NO_TRADE':return {'allowed':False,'reason':'no_trade'}
    selected=MAPPING.get(decision['mode'])
    if policy==FUTURES_VERSION:
        if decision['mode']=='RANGE':return {'allowed':False,'reason':'range_strategy_not_qualified'}
        selected={'TREND_UP':('futures','trend1','LONG'),'TREND_DOWN':('futures','trend1','SHORT')}.get(decision['mode'])
    if selected is None:return {'allowed':False,'reason':'unknown_regime'}
    if market is not None and market!=selected[0]:return {'allowed':False,'reason':'market_policy_absent'}
    if not eligible:return {'allowed':False,'reason':'research_eligibility_missing'}
    return {'allowed':True,'market':selected[0],'strategy':selected[1],'direction':selected[2],'policy':policy}

def closed_bar_signal(rows,strategy,*,now_ms):
    """Use only supplied closed, continuous UTC 15m bars and fixed rules."""
    if strategy not in ('trend1','range') or not rows:raise ValueError('invalid_strategy')
    bar=900000
    if any(not isinstance(r,(tuple,list)) or len(r)!=5 or type(r[0]) is not int or r[0]%bar or
           any(type(v) not in (int,float) or not math.isfinite(v) for v in r[1:]) or
           not 0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2] for r in rows):raise ValueError('invalid_candle')
    if any(b[0]-a[0]!=bar for a,b in zip(rows,rows[1:])):raise ValueError('candle_gap')
    if type(now_ms) is not int or rows[-1][0]+bar>now_ms:raise ValueError('unclosed_candle')
    return signals(rows,strategy)[-1]
