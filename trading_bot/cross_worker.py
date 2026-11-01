"""Offline cross command worker; no network calls or Telegram polling."""
import json
import time
from decimal import Decimal
from .cross_trade_report import cross_trade_report
from .paper_wallet_live_report import live_report
from .telegram_reports import when


def _status(shared, now):
    report=live_report(shared,now=now);cross=report['cross']
    if cross is None:raise ValueError('Cross not activated')
    def fmt(v):return 'неизвестно' if v is None else str(v)
    state=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]
    settings=shared.db.execute('SELECT leverage,interval FROM cross_settings WHERE id=1').fetchone()
    lines=['Futures cross • PAPER',('Входы на паузе' if state else 'Входы разрешены')+', '+settings['interval']+', плечо '+str(settings['leverage'])+'×.',
           'Капитал '+fmt(cross['equity'])+' USDT; свободно '+fmt(cross['free'])+'; маржа '+fmt(cross['margin'])+'; обязательства '+fmt(-cross['liability'])+'.',
           'Открытый результат '+fmt(cross['gross'])+'; funding '+fmt(cross['funding'])+'; запас риска '+fmt(cross['buffer'])+'.']
    if shared.db.execute("SELECT value FROM cross_runtime WHERE key='error'").fetchone()[0]!='null':lines.append('Последняя ошибка: требуется проверка данных или учёта.')
    for p in cross['positions']:lines.append(p['symbol']+' '+p['direction']+' '+p['quantity']+'; вход '+p['entry']+'.')
    for symbol in ('BTCUSDT','ETHUSDT'):
        q=shared.db.execute('SELECT observed,funding_complete FROM cross_quotes WHERE symbol=?',(symbol,)).fetchone()
        lines.append(symbol+': '+(when(q['observed'])+(' — данные неполны или устарели' if not q['funding_complete'] or not 0<=now-q['observed']<=120 else '') if q else 'нет данных'))
    if report['incomplete']:lines.append('Неполные данные: '+'; '.join(report['incomplete']))
    return '\n'.join(lines)


def process_cross(queue, receiver, book, *, started=0, now=None, clock=time.time, fault=lambda point:None):
    now=clock() if now is None else now
    for job in queue.pending('futures_cross')[:20]:
        command=job['command'];payload=json.loads(job['payload']);uid=job['update_id']
        if command=='/resume' or command=='/confirmed_close' or command.startswith(('/timeframe ','/leverage ')):
            prior_close=command=='/confirmed_close' and book.db.execute('SELECT 1 FROM cross_operations WHERE id=?',('telegram:'+str(uid),)).fetchone()
            if payload.get('received_at',0)<started and not prior_close:
                queue.acknowledge(job['id'],'Команда принята до перезапуска; отправьте новую.',applied=False);continue
        if command in ('/pause','/resume','/timeframe','/leverage') or command.startswith(('/timeframe ','/leverage ')):
            if command!='/pause' and payload.get('expires') is not None and now>=payload['expires']:
                queue.acknowledge(job['id'],'Команда устарела.',applied=False);continue
            try:result=receiver.apply(uid,command,payload,now=now)
            except ValueError:
                book.fail_closed();queue.acknowledge(job['id'],'Ошибка учёта; входы на паузе.',applied=False);continue
            fault('after_destination_commit')
            applied=command!='/pause' or bool(book.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0])
            queue.acknowledge(job['id'],result,applied=applied);continue
        if command=='/close':
            p=book.db.execute('SELECT * FROM cross_positions WHERE symbol=?',(payload.get('symbol'),)).fetchone()
            valid=bool(p and p['id']==payload.get('position') and p['quantity']==payload.get('quantity') and now<payload.get('expires',0))
            try:
                if valid:
                    with book.shared.transaction():
                        book._check();book._current_marks(book.stored_quotes(),now)
            except ValueError:valid=False
            if not valid:queue.acknowledge(job['id'],'Позиция изменилась или данные неполны; отправьте новую команду.',applied=False)
            else:queue.add_proposal(uid,'futures_cross',p['id'],p['quantity'],now=now,ttl=payload['expires']-now)
            continue
        if command=='/confirmed_close':
            operation='telegram:'+str(uid)
            prior=book.db.execute('SELECT 1 FROM cross_operations WHERE id=?',(operation,)).fetchone()
            if not prior and now>=payload.get('expires',0):
                queue.acknowledge(job['id'],'Подтверждение истекло; сделка не выполнена.',applied=False);continue
            try:
                result=book.close(operation,payload['symbol'],payload['position'],payload['quantity'],book.stored_quotes(),now=now,confirm_expires=payload['expires'],clock=clock)
            except (KeyError,ValueError):
                queue.acknowledge(job['id'],'Позиция изменилась или данные неполны; сделка не выполнена.',applied=False);continue
            fault('after_destination_commit')
            queue.acknowledge(job['id'],'Виртуальная позиция '+payload['symbol']+' закрыта; учёт сохранён.');continue
        if command in ('/report','/status','/brief_status'):
            try:result=cross_trade_report(book.shared,now=now) if command=='/report' else _status(book.shared,now)
            except ValueError:
                book.fail_closed();queue.acknowledge(job['id'],'Отчёт недоступен: требуется проверка учёта.',applied=False);continue
            queue.acknowledge(job['id'],result);continue
        if command=='/signal':
            rows=book.db.execute('SELECT symbol,candle,payload FROM cross_signals ORDER BY symbol').fetchall() if book.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_signals'").fetchone() else []
            text='\n'.join(r['symbol']+': '+json.loads(r['payload'])['action']+'; свеча '+when(r['candle']/1000) for r in rows) or 'Сигнал ещё не рассчитан.'
            queue.acknowledge(job['id'],text);continue
        if command=='/history':
            rows=book.db.execute("SELECT created,details FROM ledger_events WHERE kind='CROSS_CLOSE' ORDER BY created DESC,id DESC LIMIT 10").fetchall()
            text='\n'.join(when(r['created'])+' | '+(p:=json.loads(r['details']))['symbol']+' | gross '+p['gross']+' USDT; funding '+p['funding'] for r in rows) or 'История закрытий пуста.'
            queue.acknowledge(job['id'],text+'\nОбщий результат и комиссии: /report.');continue
        queue.acknowledge(job['id'],'Команда cross не поддерживается.',applied=False)
