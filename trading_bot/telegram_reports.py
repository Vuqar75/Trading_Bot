"""Human-readable Russian reports; all times are Baku, no network calls."""
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal


def when(stamp):
    return datetime.fromtimestamp(stamp, timezone(timedelta(hours=4))).strftime('%d.%m %H:%M:%S') + ' (Баку)'


def status(store):
    identity = store.get('identity')
    pos = store.get('position')
    quote = store.get('quote')
    mode = 'Симуляция (виртуальные деньги)' if identity['mode'] == 'paper' else 'Spot Testnet (тестовые средства)'
    lines = [mode, 'Пара: BTCUSDT | Рынок: Spot | Без плеча',
             'Таймфрейм: ' + store.get('selected_interval', identity.get('interval', '15m')),
             'Размер входа: до ' + identity.get('amount', '10') + ' USDT',
             'Новые входы: ' + ('на паузе' if store.get('paused') else 'разрешены')]
    qty = Decimal(pos['quantity'])
    lines.append('Позиция: нет' if not qty else f"Позиция: LONG {qty:.8f} BTC\nСредняя себестоимость: {Decimal(pos['cost'])/qty:.2f} USDT/BTC")
    if quote:
        age = max(0, int(time.time()-quote['time']))
        lines.append(f"Котировка: {Decimal(quote['price']):.2f} USDT\nПолучена: {when(quote['time'])}, {age} сек. назад" + (' — УСТАРЕЛА' if age > 120 else ''))
        if qty:
            estimate = qty*Decimal(quote['price'])*Decimal('.999')-Decimal(pos['cost'])
            lines.append(f'Оценка результата закрытия с комиссией 0,1%: {estimate:+.4f} USDT')
    else:
        lines.append('Котировка: ещё не получена')
    lines += [f"Накопленный реализованный PnL: {Decimal(store.get('pnl')):+.4f} USDT",
              'Комиссии: учтены' if pos['fees_known'] else 'Есть неоценённые комиссии; PnL предварительный',
              f'Незавершённых ордеров: {len(store.pending())}',
              'Блокировка: ' + (str(store.get('blocked')) if store.get('blocked') else 'нет')]
    error = store.get('error')
    lines.append('Последняя ошибка: ' + ((error.get('reason') or error.get('type', 'неизвестна')) if error else 'нет'))
    return '\n'.join(lines)


def signal_report(store):
    snap = store.get('signal')
    if not snap:
        return 'Сигнал ещё не рассчитан. Дождитесь следующего цикла проверки.'
    action = snap.get('action', 'HOLD')
    lines = ['Стратегия: пересечение SMA 10/30', 'Источник: Binance Spot Testnet',
             'BTCUSDT | Таймфрейм: ' + snap.get('interval', '15m')]
    if snap.get('time'):
        age = max(0, int(time.time()-snap['time']))
        lines.append(f"Проверка: {when(snap['time'])}, {age} сек. назад" + (' — УСТАРЕЛА' if age > 120 else ''))
    if snap.get('candle') is not None:
        lines.append('Начало закрытой свечи: ' + when(snap['candle']/1000))
    if 'fast' in snap:
        lines += [f"Предыдущие: SMA 10 = {snap['old_fast']:.2f}; SMA 30 = {snap['old_slow']:.2f}",
                  f"Последние: SMA 10 = {snap['fast']:.2f}; SMA 30 = {snap['slow']:.2f}"]
    lines.append('Сигнал: ' + {'BUY': 'пересечение вверх — покупка', 'SELL': 'пересечение вниз — выход из LONG', 'HOLD': 'нет нового пересечения'}.get(action, action))
    qty = Decimal(store.get('position')['quantity'])
    if store.pending() or store.get('blocked'):
        lines.append('Ограничение: требуется завершить сверку или устранить блокировку.')
    elif action == 'BUY' and store.get('paused'):
        lines.append('Вход по этому сигналу запрещён: включена пауза.')
    elif action == 'BUY' and qty:
        lines.append('Новый вход не нужен: позиция уже открыта.')
    elif action == 'SELL' and not qty:
        lines.append('Закрывать нечего; SHORT на споте не открывается.')
    elif store.get('last_candle') == snap.get('candle'):
        lines.append('Свеча уже обработана; повторной сделки по ней не будет.')
    lines += ['Новые входы сейчас: ' + ('на паузе' if store.get('paused') else 'разрешены'),
              'Это сигнал свечи, не подтверждение сделки. Исполнение смотрите в /status и /history.']
    return '\n'.join(lines)
