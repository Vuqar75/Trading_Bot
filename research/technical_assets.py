"""Allowlisted technical-analysis assets and compatible sidecar names. No IO."""
from pathlib import Path

SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
VIEWS=('patterns','plan','history')


def symbol(value):
    if not isinstance(value,str):raise ValueError('technical_symbol')
    value=value.upper()
    if value in ('BTC','ETH','SOL'):value+='USDT'
    if value not in SYMBOLS:raise ValueError('technical_symbol')
    return value


def sidecar(directory,asset='BTCUSDT',kind='technical'):
    asset=symbol(asset)
    if kind not in ('technical','technical-history'):raise ValueError('technical_sidecar_kind')
    return Path(directory)/(kind+('' if asset=='BTCUSDT' else '-'+asset)+'.json')


def history_matches(value,asset):
    """Only BTC can inherit a pre-multi-asset, untagged history file."""
    asset=symbol(asset)
    tagged=value.get('symbol')
    if tagged!=asset and not (tagged is None and asset=='BTCUSDT'):raise ValueError('history_market')
    if any(r['packet']['symbol']!=asset for r in value['records']):raise ValueError('history_market')
    return value
