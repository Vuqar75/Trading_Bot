"""Offline prototype for cautious valuation of an imported paper ledger.

This report is not integrated into Telegram or trading; position metadata is the
opening snapshot and becomes stale as soon as the trading stores change.
"""
import json
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo
from .paper_wallet_ledger import amount, encode


def _quote(quote, now, max_age):
    if not isinstance(quote,dict): return None
    try:
        stamp=float(quote['time']); price=amount(quote['price'],positive=True)
        if not 0 <= now-stamp <= max_age:return None
        return price
    except (KeyError,ValueError,TypeError,OverflowError):
        return None


def balance_report(ledger, *, spot_quote=None, futures_quote=None, now, period='all', max_age=120):
    if period not in ('all','today'):
        raise ValueError('Unsupported report period')
    record=ledger.db.execute("SELECT value FROM ledger_meta WHERE key='imported'").fetchone()
    if not record:raise ValueError('Opening snapshot required')
    meta=json.loads(record[0])['position_metadata']
    spot_pos=meta['spot']; fut_pos=meta.get('futures')
    sp=_quote(spot_quote,now,max_age); fp=_quote(futures_quote,now,max_age)
    with localcontext() as ctx:
        ctx.prec=120
        cash={wallet:ledger.balance(wallet+':free') for wallet in ('reserve','spot','futures_isolated')}
        locked={wallet:ledger.balance(wallet+':reserved') for wallet in ('reserve','spot','futures_isolated')}
        btc=ledger.balance('spot:position','BTC')
        margin=ledger.balance('futures_isolated:margin')
        funding=ledger.balance('futures_isolated:funding')
        entry_fee=ledger.balance('futures_isolated:entry_fee')
        issues=[];spot_value=Decimal(0);futures_gross=Decimal(0);futures_exit_cost=Decimal(0)
        if btc:
            if sp is None:issues.append('Нет свежей Spot-цены')
            else:spot_value=btc*sp
            if not spot_pos.get('fees_known'):issues.append('Неизвестна стоимость части Spot-комиссий')
        if fut_pos:
            if fp is None:issues.append('Нет свежей Futures-цены')
            if fut_pos.get('entry_fee') is None:issues.append('Неизвестна комиссия входа Futures')
            identity=meta.get('futures_identity',{})
            if identity.get('fee_bps') is None or identity.get('slippage_bps') is None:issues.append('Нет параметров модельных издержек Futures')
            if fp is not None and identity.get('fee_bps') is not None and identity.get('slippage_bps') is not None:
                qty=amount(fut_pos['quantity'],positive=True);entry=amount(fut_pos['entry'],positive=True)
                direction=fut_pos.get('direction')
                if direction not in ('LONG','SHORT'):raise ValueError('Unknown futures direction')
                fee=amount(identity['fee_bps']);slip=amount(identity['slippage_bps'])
                futures_gross=(fp-entry)*qty*(1 if direction=='LONG' else -1)
                exit_price=fp*(1+(1 if direction=='SHORT' else -1)*slip/10000)
                futures_exit_cost=qty*exit_price*fee/10000+qty*fp*slip/10000
        isolated=max(Decimal(0),margin+funding+entry_fee+futures_gross-futures_exit_cost) if fut_pos and fp is not None else margin+funding+entry_fee
        total=sum(cash.values(),Decimal(0))+sum(locked.values(),Decimal(0))+spot_value+isolated
        today=datetime.fromtimestamp(now,ZoneInfo('Asia/Baku')).date()
        transfers=[]
        for row in ledger.db.execute("SELECT created,details FROM ledger_events WHERE kind='TRANSFER' ORDER BY created,id"):
            if period=='today' and datetime.fromtimestamp(row['created'],ZoneInfo('Asia/Baku')).date()!=today:continue
            transfers.append(json.loads(row['details']))
        reconciliation=ledger.reconcile()
        if not reconciliation['balanced']:issues.append('Проводки не прошли сверку')
        valuation_incomplete = bool(issues)
        issues.append('Оценка использует только состояние на момент импортированного снимка; с торговыми базами не сверена')
        result={'wallets':{w:{'free':encode(cash[w]),'reserved':encode(locked[w])} for w in cash},
                'spot_btc':encode(btc),'spot_value_usdt':encode(spot_value) if sp is not None or not btc else None,
                'futures_margin':encode(margin),'futures_funding':encode(funding),'futures_gross_open':encode(futures_gross) if fp is not None and fut_pos else None,
                'futures_entry_fee':encode(-entry_fee) if not fut_pos or fut_pos.get('entry_fee') is not None else None,
                'spot_realized_pnl':meta.get('spot_realized_pnl') if period=='all' else None,
                'futures_realized_pnl':meta.get('futures_realized_pnl') if period=='all' else None,
                'historical_fees':None,'external_flows':None,'internal_transfers':transfers,'period':period,
                'estimated_snapshot_equity':encode(total) if sp is not None or not btc else None,
                'complete':False,'issues':issues,'reconciliation':reconciliation}
        if valuation_incomplete:result['estimated_snapshot_equity']=None
        return result


def render_balance_report(data):
    lines=['[Paper] Отчёт по виртуальным средствам (снимок, не live)',
           'Общий капитал (оценка): '+(data['estimated_snapshot_equity']+' USDT' if data['estimated_snapshot_equity'] is not None else 'неизвестно')]
    for wallet,values in data['wallets'].items():
        lines.append(f"{wallet}: свободно {values['free']} USDT, зарезервировано {values['reserved']} USDT")
    lines += ['Spot BTC: '+data['spot_btc'],'Маржа futures: '+data['futures_margin']+' USDT',
              'Funding открытой позиции: '+data['futures_funding']+' USDT',
              'Исторические комиссии и внешние движения: неизвестно',
              'Внутренних переводов за период: '+str(len(data['internal_transfers'])),
              'Сверка проводок: '+('пройдена' if data['reconciliation']['balanced'] else 'ошибка'),
              'Данные неполные: '+'; '.join(data['issues'])]
    return '\n'.join(lines)
