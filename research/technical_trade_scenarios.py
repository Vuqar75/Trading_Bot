"""Conditional price/cost arithmetic for the existing pattern model, no execution.

The normalization is NOT an order size. Candle close is NOT an executable quote.
These two barrier scenarios are NOT a forecast or a replay of future fills.
"""
from decimal import Decimal, InvalidOperation, localcontext

VERSION='pattern-cost-scenarios-v1'
PATTERNS=('engulf_long','engulf_short','pin_long','pin_short',
          'inside_long','inside_short','break_long','break_short')


def _number(value):
    if type(value) not in (int,float,str):raise ValueError('scenario_number')
    try:value=Decimal(str(value))
    except InvalidOperation:raise ValueError('scenario_number') from None
    if not value.is_finite() or value>Decimal('1e12') or value<Decimal('-1e12') or value.as_tuple().exponent < -30:
        raise ValueError('scenario_number')
    return value


def build(packet):
    if packet.get('venue')!='binance-usdm' or packet.get('symbol') not in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        raise ValueError('scenario_market')
    if packet.get('feature_version')!='technical-patterns-v1' or packet.get('limits',{}).get('orders_enabled') is not False:
        raise ValueError('scenario_contract')
    signal=packet.get('signal_ms');asof=packet.get('as_of_ms');expiry=packet.get('expires_ms')
    if any(type(x) is not int for x in (signal,asof,expiry)) or signal<=0 or signal%900000 or expiry!=signal+900000 or not signal<=asof<expiry:
        raise ValueError('scenario_time')
    flags=packet.get('detected_patterns')
    if not isinstance(flags,list) or any(x not in PATTERNS for x in flags) or len(set(flags))!=len(flags):
        raise ValueError('scenario_patterns')
    long=any(x.endswith('_long') for x in flags);short=any(x.endswith('_short') for x in flags)
    side='LONG' if long and not short else 'SHORT' if short and not long else 'WAIT'
    if packet.get('side')!=side:raise ValueError('scenario_side')
    close=_number(packet.get('close'))
    if close<=0:raise ValueError('scenario_price')
    ml=packet.get('ml')
    if not isinstance(ml,dict) or ml.get('trade_allowed') is not False or ml.get('action') not in ('CANDIDATE','NO_TRADE'):
        raise ValueError('scenario_admission')
    if ml['action']=='CANDIDATE' and (side=='WAIT' or not Decimal('.60')<=_number(ml.get('p_win_estimate'))<=1 or _number(ml.get('expected_stress_net_return'))<Decimal('.0005')):
        raise ValueError('scenario_model_gate')
    result={'version':VERSION,'symbol':packet['symbol'],'signal_ms':signal,'side':side,
            'price_basis':'last_closed_candle_not_execution_quote','reference_close':str(close),
            'reference_notional_usdt':'100','notional_is_order_size':False,
            'orders_enabled':False,'funding_included':False,'filters_verified':False,
            'model_candidate':ml['action']=='CANDIDATE','scenarios':[]}
    if side=='WAIT':
        result['status']='no_unambiguous_pattern'
        return result
    atr=_number(packet['features']['atr_pct'])
    if not 0<atr<1:raise ValueError('scenario_atr')
    with localcontext() as ctx:
        ctx.prec=28
        distance=max(Decimal('.005'),min(Decimal('.02'),Decimal('1.5')*atr))
        result.update(status='conditional_only',stop_distance_pct=str(distance*100),hold_limit_minutes=240)
        sign=Decimal(1 if side=='LONG' else -1);qty=Decimal('100')/close;fee=Decimal('.0005')
        for name,impact,delay in (('base',Decimal('.0005'),1),('stress',Decimal('.001'),6)):
            entry=close*(1+sign*impact)
            stop=entry*(1-sign*distance);target=entry*(1+sign*2*distance)
            def net(reference):
                exit_fill=reference*(1-sign*impact)
                return qty*(sign*(exit_fill-entry)-fee*(entry+exit_fill))
            loss=net(stop);win=net(target)
            if not loss<0<win:raise ValueError('scenario_barriers')
            result['scenarios'].append({'name':name,'assumed_delay_minutes':delay,
                'fee_bps_per_side':'5','impact_bps_per_side':str(impact*10000),
                'assumed_entry':str(entry),'stop_reference':str(stop),'target_reference':str(target),
                'net_at_stop_usdt_per_100':str(loss),'net_at_target_usdt_per_100':str(win),
                'net_reward_risk':str(win/-loss),
                'two_outcome_breakeven_win_rate_pct':str(-loss/(win-loss)*100)})
    return result


def validate(value,packet):
    """Recompute from the bound candle/ATR/side; reject amended levels or costs."""
    if value!=build(packet):raise ValueError('scenario_binding')
    return value
