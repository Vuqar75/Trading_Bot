"""Plain-text trade receipts, built only from committed execution data."""
import time
from decimal import Decimal
from .telegram_reports import when
from .display_amount import display_amount as fmt

REASONS = {'SIGNAL': 'Пересечение SMA 10/30', 'STOP': 'Стоп-лосс', 'TAKE': 'Тейк-профит',
           'MANUAL': 'Команда /close', 'REVERSAL': 'Обратное пересечение SMA',
           'MODEL_LIQUIDATION': 'Модельная ликвидация', 'TEST': 'Проверка', 'DEMO': 'Демонстрация',
           'DONCHIAN60_ENTRY': 'Дневной пробой Donchian60', 'DONCHIAN60_EXIT': 'Дневной выход Donchian60'}
STATUSES = {'PARTIALLY_FILLED': 'частично исполнен', 'NEW': 'принят', 'PENDING_CANCEL': 'ожидает отмены', 'FILLED': 'исполнен полностью', 'CANCELED': 'отменён', 'REJECTED': 'отклонён',
            'EXPIRED': 'истёк', 'EXPIRED_IN_MATCH': 'истёк при сопоставлении'}


def fees_text(fees):
    return ', '.join(f'{fmt(value,asset)} {asset}' for asset, value in sorted(fees.items())) or '0'


def spot_receipt(*, mode, side, status, quantity, average, fees, remaining, reason,
                 interval, client, entry=None, entry_is_cost=False, entry_fees=None, net='0', known=True, symbol='BTCUSDT'):
    qty = Decimal(str(quantity))
    asset=symbol[:-4]
    label = 'Виртуальная' if mode == 'paper' else 'Testnet'
    if not qty:
        heading = f'⚠️ {label} заявка: {STATUSES.get(status, status)}'
    elif side == 'BUY':
        heading = f'🟢 {label} покупка: '+('исполнена полностью' if status == 'FILLED' else 'частичное исполнение')
    else:
        heading = '🔴 '+label+' позиция '+('закрыта' if Decimal(str(remaining)) == 0 else 'частично закрыта')
    lines = [heading, f'{symbol} • Spot • LONG • {interval}', when(time.time()),
             'Статус ордера: '+STATUSES.get(status, status)]
    if qty:
        lines += [f"Исполнено по ордеру: {fmt(qty,asset)} {asset}", f"Средняя цена {'покупки' if side == 'BUY' else 'выхода'}: {fmt(average)} USDT"]
        if side == 'SELL' and entry is not None:
            lines.append(('Вход (себестоимость): ' if entry_is_cost else 'Цена входа: ')+f'{fmt(entry)} USDT')
        lines.append('Комиссии этого ордера: '+fees_text(fees))
        if side == 'SELL' and entry_fees is not None:
            lines.append('Комиссии первоначальной покупки (всей): '+fees_text(entry_fees))
        if side == 'SELL':
            lines.append(f'Результат с учтёнными комиссиями: {fmt(net,signed=True)} USDT'+(' (предварительный: есть неоценённая комиссия)' if not known else ''))
        lines.append(f"Остаток позиции: {fmt(remaining,asset)} {asset}")
    lines += ['Причина: '+REASONS.get(reason, reason), 'Ордер: '+client]
    return '\n'.join(lines)


def futures_receipt(event, pos, *, interval, payload=None):
    p = payload or pos
    symbol=p.get('symbol','BTCUSDT');asset=symbol[:-4]
    lines = [('🟢 Открыта' if event == 'OPEN' else '🔴 Закрыта')+' виртуальная позиция',
             f"{symbol} • Futures isolated • {p['direction']} • {p['leverage']}× • {interval}",
             when((p['opened_ms'] if event == 'OPEN' else p['closed_ms'])/1000),
             f"Маржа: {fmt(p['margin'])} USDT",
             f"Количество: {fmt(p['quantity'],asset)} {asset}",
             f"Цена входа: {fmt(p['entry'])} USDT"]
    if event == 'OPEN':
        lines += [f"Номинал входа: {fmt(Decimal(p['entry'])*Decimal(p['quantity']))} USDT",
                  f"Комиссия входа: {fmt(p['entry_fee'])} USDT", 'Причина: сигнал стратегии']
    else:
        lines += [f"Цена выхода: {fmt(p['exit'])} USDT",
                  f"PnL движения цены: {fmt(p['gross'],signed=True)} USDT",
                  f"Комиссии входа и выхода: {fmt(p['fees'])} USDT",
                  f"Funding: {fmt(p['funding'],signed=True)} USDT",
                  f"Итоговый PnL: {fmt(p['net'],signed=True)} USDT",
                  'Причина: '+REASONS.get(p['reason'], p['reason'])]
        if Decimal(p.get('cap_adjustment', '0')):
            lines.append('Применено модельное ограничение убытка изолированной маржой.')
    lines.append('Только симуляция. Реальные деньги не используются.')
    return '\n'.join(lines)
