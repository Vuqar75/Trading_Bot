"""Pure Donchian60 daily Spot rules; no I/O or order submission."""
from decimal import Decimal,InvalidOperation

DAY_MS=86400000
WINDOW_MS=300000

def closed_days(rows,server_ms):
    if type(server_ms) is not int or not isinstance(rows,list):raise ValueError('invalid_clock_or_candles')
    candles=[]
    for r in rows:
        try:
            start,end=r[0],r[6]
            if type(start) is not int or type(end) is not int or start%DAY_MS or end!=start+DAY_MS-1:raise ValueError('invalid_day_boundary')
            close=Decimal(str(r[4]))
            if not close.is_finite() or close<=0:raise ValueError('invalid_close')
        except (IndexError,TypeError,InvalidOperation):raise ValueError('invalid_candle') from None
        if end<server_ms:candles.append((start,close))
    if len(candles)<61:raise ValueError('insufficient_warmup')
    if any(b[0]-a[0]!=DAY_MS for a,b in zip(candles,candles[1:])):raise ValueError('discontinuous_days')
    if candles[-1][0]!=(server_ms//DAY_MS-1)*DAY_MS:raise ValueError('latest_day_missing')
    return candles

def decide(closes,*,position_open,prior_level=None):
    try:values=[Decimal(x) for x in closes]
    except (TypeError,InvalidOperation):raise ValueError('invalid_close') from None
    if len(values)<61 or any(not x.is_finite() or x<=0 for x in values):raise ValueError('insufficient_valid_closes')
    midpoint=(max(values[-60:])+min(values[-60:]))/Decimal(2)
    if not position_open:
        if prior_level is not None:raise ValueError('cash_with_exit_level')
        return ('BUY',midpoint) if values[-1]>max(values[-61:-1]) else ('HOLD',None)
    try:old=Decimal(prior_level)
    except (TypeError,InvalidOperation):raise ValueError('missing_exit_level') from None
    if not old.is_finite() or old<=0:raise ValueError('invalid_exit_level')
    if values[-1]<old:return 'SELL',old
    return 'HOLD',max(old,midpoint)

def validate_execution(*,boundary_ms,signal_commit_ms,request_ms,trade_ms,received_ms,server_before_ms,server_after_ms):
    values=(boundary_ms,signal_commit_ms,request_ms,trade_ms,received_ms,server_before_ms,server_after_ms)
    if any(type(v) is not int for v in values):raise ValueError('time_unknown')
    if boundary_ms%DAY_MS or not boundary_ms<=signal_commit_ms<=boundary_ms+WINDOW_MS:raise ValueError('signal_not_timely')
    if not signal_commit_ms<request_ms<=received_ms<=boundary_ms+WINDOW_MS:raise ValueError('execution_not_timely')
    if not signal_commit_ms<trade_ms<=received_ms or received_ms-trade_ms>5000:raise ValueError('trade_not_fresh')
    if server_after_ms<server_before_ms or abs(server_before_ms-request_ms)>5000 or abs(server_after_ms-received_ms)>5000:raise ValueError('clock_not_confirmed')
