"""Sizing preview for isolated study only; does not place or reserve orders."""
from decimal import Decimal, ROUND_CEILING
from math import gcd
from trading_bot.paper_pair_catalog import usd_m_perpetual_candidate

MAX_NOTIONAL=Decimal('100')
MAX_RISK=Decimal('5')
# Stress reserve, not a prediction: 21 eight-hour events over maximum 7-day hold,
# at 10 bps each. Actual rates/timing and gap loss can exceed this estimate.
FUNDING_STRESS_BPS=Decimal('210')

def validate_market_take_bound(info,execution_price,mark_price):
 if not isinstance(info,dict) or info.get('symbol')!='BTCUSDT' or info.get('contractType')!='PERPETUAL' or info.get('marginAsset')!='USDT':raise ValueError('invalid_contract')
 try:bound=Decimal(str(info['marketTakeBound']));price=Decimal(str(execution_price));mark=Decimal(str(mark_price))
 except (KeyError,ArithmeticError,TypeError):raise ValueError('market_take_bound_unavailable') from None
 if not all(x.is_finite() for x in (bound,price,mark)) or not 0<bound<1 or price<=0 or mark<=0:raise ValueError('market_take_bound_invalid')
 if not mark*(1-bound)<=price<=mark*(1+bound):raise ValueError('market_take_bound_exceeded')
 return bound

def minimum_futures_size(info, price, *, stop_distance, fee_bps, slippage_bps,filter_price=None):
 price=Decimal(str(price));filter_price=price if filter_price is None else Decimal(str(filter_price));stop=Decimal(str(stop_distance));fee=Decimal(str(fee_bps));slip=Decimal(str(slippage_bps))
 if not all(x.is_finite() for x in (price,filter_price,stop,fee,slip)) or price<=0 or filter_price<=0 or not 0<stop<1 or not 0<=fee<=100 or not 0<=slip<=100:raise ValueError('invalid_sizing_input')
 take_bound=validate_market_take_bound(info,price,filter_price)
 rows=info.get('filters');
 if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):raise ValueError('invalid_filters')
 names=[x.get('filterType') for x in rows]
 if len(names)!=len(set(names)):raise ValueError('duplicate_filters')
 by={x['filterType']:x for x in rows}
 if 'LOT_SIZE' not in by or 'MARKET_LOT_SIZE' not in by or 'MIN_NOTIONAL' not in by:raise ValueError('missing_filter')
 lots=[by['LOT_SIZE'],by['MARKET_LOT_SIZE']]
 try:
  mins=[Decimal(x['minQty']) for x in lots];steps=[Decimal(x['stepSize']) for x in lots]
  mn=Decimal(str(by['MIN_NOTIONAL'].get('notional',by['MIN_NOTIONAL'].get('minNotional'))))
  if not all(x.is_finite() and x>=0 and x<Decimal('1e20') and x.as_tuple().exponent>=-30 for x in mins+steps+[mn]) or steps[0]<=0 or mn<=0:raise ValueError('invalid_filter')
  active=[s for s in steps if s>0];scale=10**max(0,max(-s.as_tuple().exponent for s in active));units=[int(s*scale) for s in active]
  lcm=units[0]
  for n in units[1:]:lcm=lcm*n//gcd(lcm,n)
  step=Decimal(lcm)/scale
  required=max(*mins,mn/filter_price)
  quantity=(required/step).to_integral_value(rounding=ROUND_CEILING)*step
  preview=usd_m_perpetual_candidate(info,'BTCUSDT',quantity,filter_price)
  notional=quantity*price
  # Short stop and its exit fees have a larger notional than the entry.
  risk=notional*stop+notional*((fee+slip)/Decimal(10000))*(2+stop)+notional*FUNDING_STRESS_BPS/Decimal(10000)
  if notional>MAX_NOTIONAL:raise ValueError('notional_limit')
  if risk>MAX_RISK:raise ValueError('risk_limit')
  return {**preview,'execution_notional_usdt':str(notional),'market_take_bound':str(take_bound),'funding_stress_bps':str(FUNDING_STRESS_BPS),'max_notional_usdt':str(MAX_NOTIONAL),'risk_usdt':str(risk),'max_risk_usdt':str(MAX_RISK),'leverage':1,'method':'minimum_valid_market_quantity_v1'}
 except (KeyError,TypeError,ArithmeticError) as exc:raise ValueError('invalid_filters') from exc
