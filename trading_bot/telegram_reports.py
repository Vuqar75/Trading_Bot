"""Human-readable Russian reports; all times are Baku, no network calls."""
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from .user_text import block_text, error_text
from .display_amount import display_amount as fmt


def when(stamp):
    return datetime.fromtimestamp(stamp, timezone(timedelta(hours=4))).strftime('%d.%m %H:%M:%S') + ' (Баку)'

def pilot_lines(store):
    from .spot_pilot_limits import entry_allowed
    state=store.get('spot_pilot')
    if not isinstance(state,dict):return None
    allowed,reason=entry_allowed(state)
    if store.get('paused'):allowed,reason=False,'ручная или защитная пауза'
    if store.get('blocked'):allowed,reason=False,'требуется финансовая сверка'
    if Decimal(store.get('position')['quantity']):allowed,reason=False,'позиция уже открыта'
    reasons={'allowed':'лимиты соблюдены','pilot_not_active':'ожидание запуска','pilot_entry_limit':'достигнут лимит трёх входов','pilot_loss_limit':'достигнут лимит убытка','pilot_realized_unconfirmed':'результат требует сверки'}
    state_time=lambda key: when(state[key]/1000) if state.get(key) else 'ещё нет'
    close=state.get('last_close');threshold=state.get('entry_high')
    result=state.get('realized_usdt') if not state.get('realized_unconfirmed') else None
    return ['Spot • PAPER • BTCUSDT • тренд 1H (btc-trend1-48-24-v1)',
      'Экспериментальный paper-пилот; преимущество не доказано.',
      'Входы: '+('разрешены' if allowed else 'запрещены')+' — '+reasons.get(reason,reason),
      'Начало: '+state_time('start_ms')+'; последняя свеча: '+state_time('last_candle_ms'),
      'Следующий расчёт: '+state_time('next_execution_ms'),
      'Вход: закрытие выше максимума 48 предыдущих часов; закрытие / максимум: '+(fmt(close)+' / '+fmt(threshold)+' USDT' if close is not None and threshold is not None else 'ещё не рассчитаны'),
      'Последний сигнал / результат: '+str(state.get('last_action') or 'ожидание')+' / '+str(state.get('last_result') or 'ожидание'),
      'Выход: минимум 24 часов, стоп 2% или 168 часов; точная цена стопа не гарантирована.',
      'Входы пилота: '+str(len(state.get('entries',[])))+' из 3; реализованный net: '+(fmt(result,signed=True)+' USDT' if result is not None else 'не подтверждён')+'; предел убытка 1 USDT.',
      'Проверочный цикл и прежняя история не входят в результат пилота.']


def status(store):
    identity = store.get('identity')
    pos = store.get('position')
    quote = store.get('quote')
    symbol=identity.get('symbol','BTCUSDT');asset=symbol[:-4]
    if quote and quote.get('symbol','BTCUSDT')!=symbol:quote=None
    mode = 'Симуляция (виртуальные деньги)' if identity['mode'] == 'paper' else 'Spot Testnet (тестовые средства)'
    lines = [mode, 'Пара: '+symbol+' | Рынок: Spot | Без плеча',
             'Таймфрейм: ' + store.get('selected_interval', identity.get('interval', '15m')),
             'Размер входа: до ' + fmt(identity.get('amount', '10')) + ' USDT',
             'Новые входы: ' + ('на паузе' if store.get('paused') else 'разрешены')]
    pilot=pilot_lines(store)
    if pilot:lines=pilot
    donchian=None if pilot else store.get('donchian60')
    if isinstance(donchian,dict):
        lines[2]='Стратегия: Donchian60 • дневные свечи UTC; вход до 10 USDT'
        lines += ['Новый paper-этап: '+when(donchian['start_ms']/1000),
                  'Следующий дневной расчёт: '+when(donchian['next_boundary_ms']/1000),
                  'Последняя закрытая свеча: '+(when(donchian['last_candle_ms']/1000) if donchian.get('last_candle_ms') else 'ещё не обработана'),
                  'Сигнал: '+str(donchian.get('last_action') or 'ожидание сигнала')+'; уровень выхода: '+(fmt(donchian['level'])+' USDT' if donchian.get('level') else 'нет'),
                  'Защита: выход по закрытию дня; внутридневной стоп не гарантирован.',
                  'Статус расчёта: '+(str(donchian.get('error') or 'заблокирован') if donchian.get('halted') else 'работает')]
        reasons={'entry_skipped_exchange_filter':'Вход пропущен: лимит 10 USDT не проходит биржевые фильтры.',
                 'entry_skipped_paused':'Вход пропущен: рынок был на паузе.',
                 'partial_residual':'Остаток позиции требует проверки биржевых фильтров.',
                 'filled':'Последний сигнал исполнен в paper-модели.'}
        if donchian.get('last_result') in reasons:lines.append(reasons[donchian['last_result']])
        closed=store.db.execute('SELECT pnl,known FROM runtime_closes WHERE ts>=?',(donchian['start_ms']/1000,)).fetchall()
        lines.append('Реализованный PnL нового этапа: '+(fmt(sum((Decimal(x['pnl']) for x in closed),Decimal(0)),signed=True)+' USDT' if all(x['known'] for x in closed) else 'неизвестно'))
        import json
        fills=[json.loads(x[0]) for x in store.db.execute("SELECT f.payload FROM runtime_fills f JOIN runtime_orders o ON o.client_id=f.client_id WHERE o.created>=?",(donchian['start_ms']/1000,))]
        fee=sum((Decimal(f['commission']) for f in fills if f.get('commissionAsset')=='USDT'),Decimal(0))
        lines.append('Комиссии нового этапа: '+(fmt(fee)+' USDT' if all(f.get('commissionAsset')=='USDT' for f in fills) else 'неизвестны: комиссия в другом активе'))
    qty = Decimal(pos['quantity'])
    lines.append('Позиция: нет' if not qty else f"Позиция: LONG {fmt(qty,asset)} {asset}\nСредняя себестоимость: {fmt(Decimal(pos['cost'])/qty)} USDT/{asset}")
    if quote:
        age = max(0, int(time.time()-quote['time']))
        lines.append(f"Котировка: {fmt(quote['price'])} USDT\nПолучена: {when(quote['time'])}, {age} сек. назад" + (' — УСТАРЕЛА' if age > 120 else ''))
        if qty and (not (donchian or pilot) or age<=120):
            estimate = qty*Decimal(quote['price'])*Decimal('.999')*(Decimal('.9995') if donchian or pilot else Decimal(1))-Decimal(pos['cost'])
            lines.append(f'Оценка результата закрытия с комиссией 0,1%: {fmt(estimate,signed=True)} USDT')
        elif qty and (donchian or pilot):lines.append('Открытый PnL неизвестен: котировка устарела.')
    else:
        lines.append('Котировка: ещё не получена')
    lines += [f"Накопленный реализованный PnL: {fmt(store.get('pnl'),signed=True)} USDT",
              'Комиссии: учтены' if pos['fees_known'] else 'Есть неоценённые комиссии; PnL предварительный',
              f'Незавершённых ордеров: {len(store.pending())}',
              'Блокировка: ' + block_text(store.get('blocked'))]
    error = store.get('error')
    lines.append('Последняя ошибка: ' + error_text(error))
    if store.get('telegram_error'):
        lines.append('Доставка Telegram: были ошибки; очередь сообщений сохранена.')
    return '\n'.join(lines)


def signal_report(store):
    pilot=pilot_lines(store)
    if pilot:return '\n'.join(pilot)
    state=store.get('donchian60')
    if isinstance(state,dict):
        return '\n'.join(['Spot • PAPER • Donchian60 • BTCUSDT • дневные свечи UTC',
          'Сигнал: '+str(state.get('last_action') or 'ожидание сигнала'),
          'Последняя свеча: '+(when(state['last_candle_ms']/1000) if state.get('last_candle_ms') else 'ещё нет'),
          'Следующий расчёт: '+when(state['next_boundary_ms']/1000),
          'Уровень выхода: '+(fmt(state['level'])+' USDT' if state.get('level') else 'нет'),
          'Состояние: '+(str(state.get('error') or 'заблокирован') if state.get('halted') else 'ожидание расчёта'),
          'Дневной выход по закрытию; внутридневное исполнение не гарантируется.'])
    snap = store.get('signal')
    if not snap:
        return 'Сигнал ещё не рассчитан. Дождитесь следующего цикла проверки.'
    symbol=store.get('identity').get('symbol','BTCUSDT')
    if snap.get('symbol','BTCUSDT')!=symbol:
        return 'Сигнал для выбранной пары ещё не рассчитан.'
    action = snap.get('action', 'HOLD')
    lines = ['Стратегия: пересечение SMA 10/30', 'Источник: Binance Spot Testnet',
             symbol+' | Таймфрейм: ' + snap.get('interval', '15m')]
    if snap.get('time'):
        age = max(0, int(time.time()-snap['time']))
        lines.append(f"Проверка: {when(snap['time'])}, {age} сек. назад" + (' — УСТАРЕЛА' if age > 120 else ''))
    if snap.get('candle') is not None:
        lines.append('Начало закрытой свечи: ' + when(snap['candle']/1000))
    if 'fast' in snap:
        lines += [f"Предыдущие: SMA 10 = {fmt(str(snap['old_fast']))}; SMA 30 = {fmt(str(snap['old_slow']))}",
                  f"Последние: SMA 10 = {fmt(str(snap['fast']))}; SMA 30 = {fmt(str(snap['slow']))}"]
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
    entries = ['Последние выходы LONG • Spot (включая частичные):']
    for row in rows:
        fills = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM runtime_fills WHERE client_id=?', (row['client_id'],))]
        qty = sum((Decimal(f['qty']) for f in fills), Decimal(0))
        quote = sum((Decimal(f['quoteQty']) for f in fills), Decimal(0))
        net = sum((Decimal(r[0]) for r in store.db.execute('SELECT pnl FROM runtime_closes WHERE client_id=?', (row['client_id'],))), Decimal(0))
        fees = {}
        for f in fills:
            asset = f['commissionAsset']
            fees[asset] = fees.get(asset, Decimal(0))+Decimal(f['commission'])
        symbol=store._order_symbol(row['client_id'],history=True);asset=symbol[:-4]
        part = [when(row['closed_at']), f"Пара: {symbol}", f"Продано: {fmt(qty,asset)} {asset}"]
        entry = store.get('entry_snapshot:'+row['client_id'])
        if entry:
            part.append(('Вход (себестоимость): ' if entry['is_cost'] else 'Цена входа: ')+f"{fmt(entry['price'])} USDT")
        else:
            part.append('Цена входа: в старой записи отдельно не сохранена')
        if qty:
            part.append(f'Средняя цена выхода: {fmt(quote/qty)} USDT')
        part += ['Комиссии продажи: '+fees_text(fees), f"Результат с учтёнными комиссиями: {fmt(net,signed=True)} USDT"+(' (предварительный)' if not row['known'] else ''), 'Причина: '+REASONS.get(row['reason'], 'Не указана')]
        entries.append('\n'.join(part))
    return '\n\n'.join(entries)


def daily_report(store, now=None):
    now = time.time() if now is None else now
    pilot=pilot_lines(store)
    if pilot:return '\n'.join(pilot+['Позиция и оценка: /status spot. Отдельное наблюдение: /donchian.'])
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=4)))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = store.db.execute('SELECT client_id,pnl,known FROM runtime_closes WHERE ts>=? AND ts<=?', (start, now)).fetchall()
    known = all(r['known'] for r in rows)
    net = sum((Decimal(r['pnl']) for r in rows), Decimal(0))
    all_known=not store.db.execute('SELECT 1 FROM runtime_closes WHERE known=0 LIMIT 1').fetchone()
    state=store.get('donchian60');label='Spot • PAPER • Donchian60 • BTCUSDT • дневные UTC • ' if isinstance(state,dict) else 'Spot • все пары • '
    result=[label+'отчёт за '+local.strftime('%d.%m.%Y')+' (Баку)',
        'Обновлён: '+when(now), f'Ордеров с выходами сегодня: {len({r["client_id"] for r in rows})} (возможны частичные)',
        f'Реализованный результат сегодня: {fmt(net,signed=True)} USDT' if known else 'Реализованный результат сегодня: неизвестен — не все комиссии оценены.',
        f"Результат за всё время: {fmt(store.get('pnl'),signed=True)} USDT" if all_known else 'Результат за всё время: неизвестен — не все комиссии оценены.',
        'Суточные комиссии и просадка: см. отчёт наблюдателя; отсутствие данных не означает ноль.']
    if isinstance(state,dict):
        stage=store.db.execute('SELECT pnl,known FROM runtime_closes WHERE ts>=? AND ts<=?',(state['start_ms']/1000,now)).fetchall()
        result += ['Начало Donchian60: '+when(state['start_ms']/1000),
                   'Реализованный PnL этапа: '+(fmt(sum((Decimal(r['pnl']) for r in stage),Decimal(0)),signed=True)+' USDT' if all(r['known'] for r in stage) else 'неизвестен'),
                   'Открытый PnL и комиссии этапа: /status. Независимое исследование: /donchian.']
    return '\n'.join(result)


def futures_daily_report(store, now=None):
    import json
    now=time.time() if now is None else now
    local=datetime.fromtimestamp(now,timezone(timedelta(hours=4)))
    start=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    closed=[(r['ts'],json.loads(r['payload'])) for r in store.db.execute("SELECT ts,payload FROM futures_events WHERE event='CLOSE'")]
    today=[p for ts,p in closed if start<=ts<=now]
    total=[p for ts,p in closed if ts<=now]
    def totals(rows):
        try:
            values=[tuple(Decimal(p[k]) for k in ('net','fees','funding')) for p in rows]
            if any(not value.is_finite() for row in values for value in row):raise ValueError()
            return tuple(sum((row[i] for row in values),Decimal(0)) for i in range(3))
        except (KeyError,ValueError,TypeError,ArithmeticError):return None
    d=totals(today);a=totals(total)
    def line(label,rows,values):
        if values is None:return label+': закрыто '+str(len(rows))+'; результат, комиссии или funding неизвестны.'
        return label+': закрыто '+str(len(rows))+f', net {fmt(values[0],signed=True)}, комиссии {fmt(values[1])}, funding {fmt(values[2],signed=True)} USDT.'
    return '\n'.join(['Futures isolated • PAPER • торговый отчёт',
        line('Сегодня по Баку ('+local.strftime('%d.%m.%Y')+')',today,d),
        line('За всё время',total,a),
        'Показаны закрытые позиции; комиссии и funding открытой позиции здесь не суммируются.'])
