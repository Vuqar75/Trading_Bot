"""Presentation only: never use rounded values for trading or accounting."""
from decimal import Decimal, ROUND_HALF_UP, localcontext


def display_amount(value, unit='USDT', *, signed=False):
    if value is None:return 'неизвестно'
    if isinstance(value,float):raise TypeError('Amounts must not use float')
    value=Decimal(value)
    if not value.is_finite():raise ValueError('Non-finite amount')
    if not value:return '0'
    places=4 if unit.upper() in ('USD','USDT','USDC') else 10
    quantum=Decimal(1).scaleb(-places)
    sign='-' if value.is_signed() else ('+' if signed else '')
    if value.copy_abs()<quantum:return sign+'меньше '+format(quantum,'f')
    with localcontext() as ctx:
        ctx.prec=max(28,len(value.as_tuple().digits)+max(0,value.adjusted())+places+2)
        rounded=value.copy_abs().quantize(quantum,rounding=ROUND_HALF_UP)
    return sign+format(rounded,'f').rstrip('0').rstrip('.')
