"""Exact paper fill quantities against an observed Spot filter snapshot.

This selects a permissible executable lot; it never rounds ledger balances,
debt, interest or an already submitted fill. It does not certify a real order.
"""
from decimal import Decimal, localcontext
from fractions import Fraction
from math import lcm

from .paper_wallet_ledger import amount


def _fraction(value):
    return Fraction(amount(value))


def _integer_floor(value):
    return value.numerator//value.denominator


def _integer_ceil(value):
    return -((-value.numerator)//value.denominator)


def _exact_decimal(value):
    """A lot step is a finite decimal, so an integer multiple stays finite."""
    with localcontext() as ctx:
        ctx.prec=120
        result=Decimal(value.numerator)/Decimal(value.denominator)
        if Fraction(result)!=value:raise ValueError('Lot is not exactly representable')
        return amount(result)


def _limits(filters):
    if not isinstance(filters,dict):raise ValueError('Spot filters unavailable')
    rules=filters.get('lot_rules')
    if not isinstance(rules,list) or not rules:raise ValueError('Spot lot filters missing')
    steps=[];minimum=Fraction(0);maximum=None
    for rule in rules:
        if not isinstance(rule,dict):raise ValueError('Invalid lot filter')
        try:step=_fraction(rule['step']);lo=_fraction(rule['min']);hi=_fraction(rule['max'])
        except (KeyError,TypeError):raise ValueError('Invalid lot filter') from None
        if min(step,lo,hi)<0 or (hi and hi<lo):raise ValueError('Invalid lot filter')
        minimum=max(minimum,lo)
        if hi:maximum=hi if maximum is None else min(maximum,hi)
        if step:steps.append(step)
    if not steps:raise ValueError('Spot lot step unavailable')
    denominator=lcm(*(s.denominator for s in steps))
    step=Fraction(lcm(*(s.numerator*(denominator//s.denominator) for s in steps)),denominator)
    if maximum is not None and maximum<minimum:raise ValueError('Conflicting Spot filters')
    try:minimum_notional=_fraction(filters['min_notional']);maximum_notional=_fraction(filters.get('max_notional','0'))
    except (KeyError,TypeError):raise ValueError('Spot notional filter missing') from None
    if minimum_notional<0 or maximum_notional<0 or (maximum_notional and maximum_notional<minimum_notional):raise ValueError('Invalid notional filter')
    return step,minimum,maximum,minimum_notional,maximum_notional


def validate_lot(quantity,mark,filters):
    quantity=_fraction(quantity);mark=_fraction(mark)
    if quantity<=0 or mark<=0:raise ValueError('Invalid paper fill')
    step,minimum,maximum,min_notional,max_notional=_limits(filters)
    if quantity%step or quantity<minimum or (maximum is not None and quantity>maximum):raise ValueError('Quantity violates Spot lot filters')
    filter_price=_fraction(filters['notional_price']) if 'notional_price' in filters else mark
    if filter_price<=0:raise ValueError('Invalid notional reference price')
    notional=quantity*filter_price
    if notional<min_notional or (max_notional and notional>max_notional):raise ValueError('Quantity violates Spot notional filters')
    return _exact_decimal(quantity)


def affordable_lot(target,available,execution_price,fee_rate,mark,filters,*,allow_partial):
    """Choose a lot to cover target; partial liquidation retains any unpaid debt.

    Full coverage uses the least lot >= target. A partial liquidation uses the
    greatest affordable lot <= target. Dust and debt are never overwritten.
    """
    target=_fraction(target);available=_fraction(available);price=_fraction(execution_price)
    fee=_fraction(fee_rate);mark=_fraction(mark)
    if target<0 or available<0 or price<=0 or fee<0 or mark<=0 or type(allow_partial) is not bool:raise ValueError('Invalid lot inputs')
    if not target:return Decimal(0)
    step,minimum,maximum,min_notional,max_notional=_limits(filters)
    filter_price=_fraction(filters['notional_price']) if 'notional_price' in filters else mark
    if filter_price<=0:raise ValueError('Invalid notional reference price')
    full=_integer_ceil(target/step)*step
    if ((maximum is None or full<=maximum) and full>=minimum and full*filter_price>=min_notional and
        (not max_notional or full*filter_price<=max_notional) and full*price*(1+fee)<=available):
        return validate_lot(_exact_decimal(full),_exact_decimal(mark),filters)
    if not allow_partial:raise ValueError('Insufficient USDT or lot limit to repay')
    cap=min(target,available/(price*(1+fee)))
    if maximum is not None:cap=min(cap,maximum)
    if max_notional:cap=min(cap,max_notional/filter_price)
    lot=_integer_floor(cap/step)*step
    if lot<=0 or lot<minimum or lot*filter_price<min_notional:return Decimal(0)
    return validate_lot(_exact_decimal(lot),_exact_decimal(mark),filters)


def sellable_lot(available,mark,filters):
    """Largest executable lot, leaving any untradeable dust in the ledger."""
    available=_fraction(available);mark=_fraction(mark)
    if available<0 or mark<=0:raise ValueError('Invalid lot inputs')
    step,minimum,maximum,min_notional,max_notional=_limits(filters)
    filter_price=_fraction(filters['notional_price']) if 'notional_price' in filters else mark
    if filter_price<=0:raise ValueError('Invalid notional reference price')
    cap=available
    if maximum is not None:cap=min(cap,maximum)
    if max_notional:cap=min(cap,max_notional/filter_price)
    lot=_integer_floor(cap/step)*step
    if lot<=0 or lot<minimum or lot*filter_price<min_notional:return Decimal(0)
    return validate_lot(_exact_decimal(lot),_exact_decimal(mark),filters)


def budget_lot(budget,execution_price,fee_rate,mark,filters):
    """Largest permitted lot whose exact price and fees fit the paper budget."""
    budget=_fraction(budget);price=_fraction(execution_price);fee=_fraction(fee_rate);mark=_fraction(mark)
    if budget<0 or price<=0 or fee<0 or mark<=0:raise ValueError('Invalid budget inputs')
    step,minimum,maximum,min_notional,max_notional=_limits(filters)
    filter_price=_fraction(filters['notional_price']) if 'notional_price' in filters else mark
    if filter_price<=0:raise ValueError('Invalid notional reference price')
    cap=budget/(price*(1+fee))
    if maximum is not None:cap=min(cap,maximum)
    if max_notional:cap=min(cap,max_notional/filter_price)
    lot=_integer_floor(cap/step)*step
    if lot<=0 or lot<minimum or lot*filter_price<min_notional:return Decimal(0)
    return validate_lot(_exact_decimal(lot),_exact_decimal(mark),filters)
