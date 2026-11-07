"""Fixed 1h BTCUSDT research rule, using only closed public 15m bars.

This module is a pure calculation and is not itself a trading authorization.
"""
from decimal import Decimal,InvalidOperation

BAR=900000
HOUR=3600000
VERSION='btc-trend1-48-24-v1'

def evaluate(rows,*,server_ms,start_ms,position_open):
    if type(server_ms) is not int or type(start_ms) is not int or type(position_open) is not bool:raise ValueError('invalid_state')
    closed=[]
    for row in rows:
        try:
            t,end=row[0],row[6]
            if type(t) is not int or t%BAR or type(end) is not int or end!=t+BAR-1:raise ValueError('invalid_boundary')
            values=[Decimal(str(x)) for x in row[1:5]]
            if any(not x.is_finite() or x<=0 for x in values):raise ValueError('invalid_price')
            o,h,l,c=values
            if not l<=min(o,c)<=max(o,c)<=h:raise ValueError('invalid_ohlc')
        except (IndexError,TypeError,InvalidOperation):raise ValueError('invalid_candle') from None
        if end<server_ms:closed.append((t,c))
    if len(closed)<198 or any(b[0]-a[0]!=BAR for a,b in zip(closed,closed[1:])):raise ValueError('insufficient_or_discontinuous_history')
    # The last closed bar is the prescribed skipped 15m interval; the bar
    # before it must close at the whole-hour signal boundary.
    skip=closed[-1];signal=closed[-2];boundary=signal[0]+BAR
    if boundary%HOUR or skip[0]!=boundary or server_ms<boundary+BAR:raise ValueError('not_execution_boundary')
    hourly=[(t,c) for t,c in closed[:-1] if (t+BAR)%HOUR==0]
    if len(hourly)<49 or hourly[-1][0]!=signal[0]:raise ValueError('hourly_history_missing')
    close=hourly[-1][1];previous=[c for _,c in hourly[-49:-1]]
    high=max(previous);exit_low=min(previous[-24:]);action='HOLD'
    if boundary>start_ms:
        if position_open and close<exit_low:action='SELL'
        elif not position_open and close>high:action='BUY'
    return {'version':VERSION,'signal_boundary_ms':boundary,'execution_boundary_ms':boundary+BAR,
            'close':str(close),'entry_high':str(high),'exit_low':str(exit_low),'action':action,
            'reason':'before_start' if boundary<=start_ms else 'no_breakout' if action=='HOLD' else 'signal'}
