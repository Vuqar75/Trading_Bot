"""Plain-text trade receipts, built only from committed execution data."""
import time
from decimal import Decimal
from .telegram_reports import when

REASONS = {'SIGNAL': 'Пересечение SMA 10/30', 'STOP': 'Стоп-лосс', 'TAKE': 'Тейк-профит',
           'MANUAL': 'Команда /close', 'REVERSAL': 'Обратное пересечение SMA',
           'MODEL_LIQUIDATION': 'Модельная ликвидация', 'TEST': 'Проверка', 'DEMO': 'Демонстрация'}
STATUSES = {'PARTIALLY_FILLED': 'частично исполнен', 'NEW': 'принят', 'PENDING_CANCEL': 'ожидает отмены', 'FILLED': 'исполнен полностью', 'CANCELED': 'отменён', 'REJECTED': 'отклонён',
            'EXPIRED': 'истёк', 'EXPIRED_IN_MATCH': 'истёк при сопоставлении'}


def fees_text(fees):
    return ', '.join(f'{Decimal(str(value)):.8f} {asset}' for asset, value in sorted(fees.items())) or '0'


def spot_receipt(*, mode, side, status, quantity, average, fees, remaining, reason,
                 interval, client, entry=None, entry_is_cost=False, entry_fees=None, net='0', known=True):
    qty = Decimal(str(quantity))
    label = 'Виртуальная' if mode == 'paper' else 'Testnet'
    if not qty:
        heading = f'⚠️ {label} заявка: {STATUSES.get(status, status)}'
    elif side == 'BUY':
        heading = f'🟢 {label} покупка: '+('исполнена полностью' if status == 'FILLED' else 'частичное исполнение')
    else:
        heading = '🔴 '+label+' позиция '+('закрыта' if Decimal(str(remaining)) == 0 else 'частично закрыта')
    lines = [heading, f'BTCUSDT • Spot • LONG • {interval}', when(time.time()),
             'Статус ордера: '+STATUSES.get(status, status)]
    if qty:
        lines += [f'Исполнено по ордеру: {qty:.8f} BTC', f"Средняя цена {'покупки' if side == 'BUY' else 'выхода'}: {Decimal(str(average)):.2f} USDT"]
        if side == 'SELL' and entry is not None:
            lines.append(('Вход (себестоимость): ' if entry_is_cost else 'Цена входа: ')+f'{Decimal(str(entry)):.2f} USDT')
        lines.append('Комиссии этого ордера: '+fees_text(fees))
        if side == 'SELL' and entry_fees is not None:
            lines.append('Комиссии первоначальной покупки (всей): '+fees_text(entry_fees))
        if side == 'SELL':
            lines.append(f'Результат с учтёнными комиссиями: {Decimal(str(net)):+.4f} USDT'+(' (предварительный: есть неоценённая комиссия)' if not known else ''))
        lines.append(f'Остаток позиции: {Decimal(str(remaining)):.8f} BTC')
    lines += ['Причина: '+REASONS.get(reason, reason), 'Ордер: '+client]
    return '\n'.join(lines)


def futures_receipt(event, pos, *, interval, payload=None):
    p = payload or pos
    lines = [('🟢 Открыта' if event == 'OPEN' else '🔴 Закрыта')+' виртуальная позиция',
             f"BTCUSDT • Futures isolated • {p['direction']} • {p['leverage']}× • {interval}",
             when((p['opened_ms'] if event == 'OPEN' else p['closed_ms'])/1000),
             f"Маржа: {Decimal(p['margin']):.2f} USDT",
             f"Количество: {Decimal(p['quantity']):.8f} BTC",
             f"Цена входа: {Decimal(p['entry']):.2f} USDT"]
    if event == 'OPEN':
        lines += [f"Номинал входа: {Decimal(p['entry'])*Decimal(p['quantity']):.2f} USDT",
                  f"Комиссия входа: {Decimal(p['entry_fee']):.4f} USDT", 'Причина: сигнал стратегии']
    else:
        lines += [f"Цена выхода: {Decimal(p['exit']):.2f} USDT",
                  f"PnL движения цены: {Decimal(p['gross']):+.4f} USDT",
                  f"Комиссии входа и выхода: {Decimal(p['fees']):.4f} USDT",
                  f"Funding: {Decimal(p['funding']):+.4f} USDT",
                  f"Итоговый PnL: {Decimal(p['net']):+.4f} USDT",
                  'Причина: '+REASONS.get(p['reason'], p['reason'])]
        if Decimal(p.get('cap_adjustment', '0')):
            lines.append('Применено модельное ограничение убытка изолированной маржой.')
    lines.append('Только симуляция. Реальные деньги не используются.')
    return '\n'.join(lines)
