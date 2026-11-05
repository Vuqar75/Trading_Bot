"""Offline Spot-USDT metadata checks for future paper pair selection.

This module does not select a market, fetch data, submit orders, or modify any
wallet. The currently deployed BTC/ETH adapters remain unchanged.
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext


class PairMetadataError(ValueError):
    """Invalid or unsupported pair/filter metadata."""


def _decimal(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if (not result.is_finite() or result < 0 or result >= Decimal('1e20')
                or (result and result < Decimal('1e-30'))
                or result.as_tuple().exponent < -60 or len(result.as_tuple().digits) > 80):
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise PairMetadataError('invalid_numeric_filter') from None


def _preview_quantity(value):
    # Worker-calculated Decimal ratios may repeat for all 120 context digits.
    # Keep that precision; untrusted textual filter inputs retain tighter limits.
    if not isinstance(value,Decimal):return _decimal(value)
    if (not value.is_finite() or value < 0 or value >= Decimal('1e20') or
        (value and value < Decimal('1e-30')) or value.as_tuple().exponent < -180 or
        len(value.as_tuple().digits)>120):
        raise PairMetadataError('invalid_numeric_filter')
    return value


def spot_usdt_candidate(info, expected_symbol, desired_quantity, reference_price):
    """Validate supplied exchangeInfo-like data; return a paper-only preview.

    All checks are offline against caller-supplied metadata and a caller-supplied
    reference price. No claim is made about its freshness or execution price.
    Quantities are rounded down only to preview filter-compatible order size;
    wallet balances and persisted position quantities are never rounded here.
    """
    if not isinstance(info, dict) or not isinstance(expected_symbol, str):
        raise PairMetadataError('invalid_metadata')
    symbol, base = info.get('symbol'), info.get('baseAsset')
    if (not isinstance(base, str) or not re.fullmatch(r'[A-Z0-9]{2,12}', base)
            or base == 'USDT' or symbol != base + 'USDT' or symbol != expected_symbol
            or info.get('quoteAsset') != 'USDT' or info.get('status') != 'TRADING'):
        raise PairMetadataError('unsupported_symbol')
    types = info.get('orderTypes')
    if not isinstance(types, list) or 'MARKET' not in types or info.get('isSpotTradingAllowed') is not True:
        raise PairMetadataError('spot_market_unavailable')
    raw = info.get('filters')
    if not isinstance(raw, list) or any(not isinstance(x, dict) or not isinstance(x.get('filterType'), str) for x in raw):
        raise PairMetadataError('invalid_filters')
    by_type = {}
    for row in raw:
        name = row['filterType']
        if name in by_type:
            raise PairMetadataError('duplicate_filter')
        by_type[name] = row
    if 'LOT_SIZE' not in by_type or not any(k in by_type for k in ('MIN_NOTIONAL', 'NOTIONAL')):
        raise PairMetadataError('required_filter_missing')
    try:
        quantity, price = _preview_quantity(desired_quantity), _decimal(reference_price)
        if not quantity or not price:
            raise PairMetadataError('nonpositive_input')
        with localcontext() as ctx:
            ctx.prec = 120
            lots = []
            for name in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
                if name not in by_type:
                    continue
                r = by_type[name]
                minimum, maximum, step = (_decimal(r[k]) for k in ('minQty', 'maxQty', 'stepSize'))
                if name == 'LOT_SIZE' and not step:
                    raise PairMetadataError('invalid_lot_step')
                if maximum and maximum < minimum:
                    raise PairMetadataError('invalid_lot_range')
                lots.append((minimum, maximum, step))
                if step:
                    quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
            if not quantity or any((step and quantity % step) or quantity < minimum or (maximum and quantity > maximum)
                                   for minimum, maximum, step in lots):
                raise PairMetadataError('quantity_outside_filters')
            notional = quantity * price
            for name in ('MIN_NOTIONAL', 'NOTIONAL'):
                if name not in by_type:
                    continue
                r = by_type[name]
                minimum = _decimal(r['minNotional'])
                maximum = _decimal(r['maxNotional']) if name == 'NOTIONAL' else Decimal(0)
                if (maximum and maximum < minimum) or notional < minimum or (maximum and notional > maximum):
                    raise PairMetadataError('notional_outside_filters')
    except KeyError:
        raise PairMetadataError('required_filter_field_missing') from None
    return {'symbol': symbol, 'base_asset': base, 'quote_asset': 'USDT',
            'quantity': format(quantity, 'f'), 'reference_notional': format(notional, 'f')}


def usd_m_perpetual_candidate(info, expected_symbol, desired_quantity, reference_price):
    """Offline filter preview for a supplied USDT-margined perpetual contract."""
    if (not isinstance(info,dict) or info.get('contractType')!='PERPETUAL' or
            info.get('marginAsset')!='USDT'):
        raise PairMetadataError('unsupported_futures_contract')
    filters=info.get('filters')
    if not isinstance(filters,list):raise PairMetadataError('invalid_filters')
    adapted=[]
    for item in filters:
        if not isinstance(item,dict):raise PairMetadataError('invalid_filters')
        row=dict(item)
        if row.get('filterType')=='MIN_NOTIONAL' and 'minNotional' not in row and 'notional' in row:
            row['minNotional']=row['notional']
        adapted.append(row)
    data=dict(info);data['filters']=adapted;data['isSpotTradingAllowed']=True
    return spot_usdt_candidate(data,expected_symbol,desired_quantity,reference_price)
