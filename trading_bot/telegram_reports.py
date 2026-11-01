"""Human-readable Russian reports; all times are Baku, no network calls."""
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from .user_text import block_text, error_text


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
              'Блокировка: ' + block_text(store.get('blocked'))]
    error = store.get('error')
    lines.append('Последняя ошибка: ' + error_text(error))
    if store.get('telegram_error'):
        lines.append('Доставка Telegram: были ошибки; очередь сообщений сохранена.')
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


def history(store):
    import json
    from .trade_notifications import REASONS, fees_text
    rows = store.db.execute("SELECT o.*,MAX(c.ts) AS closed_at,MIN(c.known) AS known FROM runtime_orders o JOIN runtime_closes c ON o.client_id=c.client_id GROUP BY o.client_id ORDER BY closed_at DESC LIMIT 10").fetchall()
    if not rows:
        return 'История выходов из позиции пуста.'
    entries = ['Последние выходы LONG • BTCUSDT Spot (включая частичные):']
    for row in rows:
        fills = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM runtime_fills WHERE client_id=?', (row['client_id'],))]
        qty = sum((Decimal(f['qty']) for f in fills), Decimal(0))
        quote = sum((Decimal(f['quoteQty']) for f in fills), Decimal(0))
        net = sum((Decimal(r[0]) for r in store.db.execute('SELECT pnl FROM runtime_closes WHERE client_id=?', (row['client_id'],))), Decimal(0))
        fees = {}
        for f in fills:
            asset = f['commissionAsset']
            fees[asset] = fees.get(asset, Decimal(0))+Decimal(f['commission'])
        part = [when(row['closed_at']), f'Продано: {qty:.8f} BTC']
        entry = store.get('entry_snapshot:'+row['client_id'])
        if entry:
            part.append(('Вход (себестоимость): ' if entry['is_cost'] else 'Цена входа: ')+f"{Decimal(entry['price']):.2f} USDT")
        else:
            part.append('Цена входа: в старой записи отдельно не сохранена')
        if qty:
            part.append(f'Средняя цена выхода: {quote/qty:.2f} USDT')
        part += ['Комиссии продажи: '+fees_text(fees), f"Результат с учтёнными комиссиями: {net:+.4f} USDT"+(' (предварительный)' if not row['known'] else ''), 'Причина: '+REASONS.get(row['reason'], 'Не указана')]
        entries.append('\n'.join(part))
    return '\n\n'.join(entries)


def daily_report(store, now=None):
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=4)))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = store.db.execute('SELECT client_id,pnl,known FROM runtime_closes WHERE ts>=? AND ts<=?', (start, now)).fetchall()
    known = all(r['known'] for r in rows)
    net = sum((Decimal(r['pnl']) for r in rows), Decimal(0))
    return '\n'.join(['Spot • BTCUSDT • отчёт за '+local.strftime('%d.%m.%Y')+' (Баку)',
        'Обновлён: '+when(now), f'Ордеров с выходами сегодня: {len({r["client_id"] for r in rows})} (возможны частичные)',
        f'Реализованный результат сегодня: {net:+.4f} USDT'+(' (предварительный: не все комиссии оценены)' if not known else ''),
        f"Результат за всё время: {Decimal(store.get('pnl')):+.4f} USDT",
        'Суточные комиссии и просадка: см. отчёт наблюдателя; отсутствие данных не означает ноль.'])


def futures_daily_report(store, now=None):
    import json
    now=time.time() if now is None else now
    local=datetime.fromtimestamp(now,timezone(timedelta(hours=4)))
    start=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    closed=[(r['ts'],json.loads(r['payload'])) for r in store.db.execute("SELECT ts,payload FROM futures_events WHERE event='CLOSE'")]
    today=[p for ts,p in closed if start<=ts<=now]
    total=[p for ts,p in closed if ts<=now]
    def totals(rows):
        return (sum((Decimal(p['net']) for p in rows),Decimal(0)),
                sum((Decimal(p['fees']) for p in rows),Decimal(0)),
                sum((Decimal(p['funding']) for p in rows),Decimal(0)))
    d=totals(today);a=totals(total)
    return '\n'.join(['Futures isolated • PAPER • торговый отчёт',
        'Сегодня по Баку ('+local.strftime('%d.%m.%Y')+'): закрыто '+str(len(today))+f', net {d[0]:+.4f}, комиссии {d[1]:.4f}, funding {d[2]:+.4f} USDT.',
        'За всё время: закрыто '+str(len(total))+f', net {a[0]:+.4f}, комиссии {a[1]:.4f}, funding {a[2]:+.4f} USDT.',
        'Показаны закрытые позиции; комиссии и funding открытой позиции здесь не суммируются.'])
