"""Offline consumer of margin command jobs; performs no network or polling."""
import json
import time
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo
from .display_amount import display_amount as fmt
from .margin_repay_proposals import MarginRepayProposals
from .paper_wallet_live_report import live_report
from .telegram_reports import when
from .margin_status_reports import loans_report,interest_report,status_report
from .status_summary import brief_status


def process_margin(queue,receiver,book,shared,*,started=0,clock=time.time,fault=lambda point:None):
    proposals=MarginRepayProposals(book.ledger,loans=book.loans)
    for job in queue.pending('margin_cross')[:20]:
        command=job['command'];payload=json.loads(job['payload']);uid=job['update_id'];now=clock()
        if (command in ('/resume','/confirmed_close','/confirm_repay') or command.startswith(('/timeframe ','/leverage ','/repay '))) and payload.get('received_at',0)<started:
            prior=(command=='/confirmed_close' and book.db.execute('SELECT 1 FROM margin_trade_operations WHERE id=?',('telegram:'+str(uid),)).fetchone())
            if command=='/confirm_repay':prior=book.db.execute("SELECT 1 FROM margin_repay_proposals WHERE id=? AND status='DONE'",(payload.get('token'),)).fetchone()
            if not prior:
                queue.acknowledge(job['id'],'Команда принята до перезапуска; отправьте новую.',applied=False);continue
        if command in ('/pause','/resume','/timeframe','/leverage') or command.startswith(('/timeframe ','/leverage ')):
            result=receiver.apply(uid,command,payload,clock_ms=lambda:int(clock()*1000))
            fault('after_destination_commit')
            applied=command!='/pause' or bool(book.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0])
            queue.acknowledge(job['id'],result,applied=applied);continue
        if command=='/close':
            p=book.db.execute('SELECT * FROM margin_positions WHERE symbol=?',(payload.get('symbol'),)).fetchone()
            valid=bool(p and p['id']==payload.get('position') and p['quantity']==payload.get('quantity') and now<payload.get('expires',0))
            try:
                if valid:
                    with book.ledger.transaction():book._check();book._current_marks(book.stored_quotes(),int(clock()*1000))
            except ValueError:valid=False
            if not valid:queue.acknowledge(job['id'],'Позиция изменилась или данные неполны; отправьте новую команду.',applied=False)
            else:queue.add_proposal(uid,'margin_cross',p['id'],p['quantity'],now=now,ttl=payload['expires']-now)
            continue
        if command=='/confirmed_close':
            try:
                book.close('telegram:'+str(uid),payload['symbol'],payload['position'],payload['quantity'],book.stored_quotes(),now_ms=int(now*1000),expires_ms=int(payload['expires']*1000),clock_ms=lambda:int(clock()*1000))
            except (KeyError,ValueError):
                queue.acknowledge(job['id'],'Позиция изменилась или данные неполны; сделка не выполнена.',applied=False);continue
            fault('after_destination_commit');queue.acknowledge(job['id'],'Виртуальная позиция '+payload['symbol']+' закрыта; учёт сохранён.');continue
        if command.startswith('/repay '):
            try:
                _,loan,quantity=command.split()
                proposal=proposals.propose('telegram:'+str(uid),loan,quantity,now_ms=int(now*1000),clock_ms=lambda:int(clock()*1000),request_expires_ms=int(payload['expires']*1000))
            except (KeyError,ValueError):
                queue.acknowledge(job['id'],'Погашение недоступно: проверьте долг, свободный актив и срок.',applied=False);continue
            fault('after_destination_commit')
            queue.acknowledge(job['id'],'Погасить '+fmt(proposal['quantity'],proposal['asset'])+' '+proposal['asset']+' из свободного остатка? /confirm '+proposal['id']+' или /cancel '+proposal['id']+'.');continue
        if command in ('/confirm_repay','/cancel_repay'):
            try:
                if command=='/confirm_repay':proposals.confirm(payload['token'],clock_ms=lambda:int(clock()*1000))
                else:proposals.cancel(payload['token'],clock_ms=lambda:int(clock()*1000))
            except (KeyError,ValueError):
                queue.acknowledge(job['id'],'Подтверждение устарело или долг изменился; сделка не выполнена.',applied=False);continue
            fault('after_destination_commit');queue.acknowledge(job['id'],'Погашение учтено.' if command=='/confirm_repay' else 'Погашение отменено.');continue
        if command in ('/status','/brief_status','/loans','/interest'):
            try:data=live_report(shared,now=clock());margin=data['margin_cross']
            except ValueError:
                book.fail_closed('LedgerError');queue.acknowledge(job['id'],'Отчёт недоступен: требуется сверка.',applied=False);continue
            if not margin:queue.acknowledge(job['id'],'Margin не активирован.',applied=False);continue
            if command=='/loans':result=loans_report(margin)
            elif command=='/interest':result=interest_report(margin,now)
            elif command=='/status':result=status_report(data,now)
            else:
                stamps=[margin['quotes'].get(s,{}).get('time_ms') for s in book.pairs]
                stamp=min(stamps)/1000 if all(type(x) is int for x in stamps) else None
                result=brief_status(paused=margin['paused'],positions=len(margin['positions']),stamp=stamp,now=now,
                                    problem=bool(margin['error'] or margin['due'] or margin['marks'] is None))
            queue.acknowledge(job['id'],result);continue
        if command=='/signal':
            rows=book.db.execute('SELECT symbol,candle,payload FROM margin_signals ORDER BY symbol').fetchall()
            queue.acknowledge(job['id'],'\n'.join(r['symbol']+': '+json.loads(r['payload'])['action']+'; свеча '+when(r['candle']/1000) for r in rows) or 'Сигнал ещё не рассчитан.');continue
        if command in ('/report','/history'):
            rows=[json.loads(r['details']) for r in book.db.execute("SELECT details FROM ledger_events WHERE kind='FINANCIAL_METADATA' AND id LIKE 'margin:position-close:%' ORDER BY created,id")]
            if command=='/history':
                text='\n'.join(when(r['at_ms']/1000)+' | '+r['symbol']+' '+r['direction']+' | итог '+fmt(r['net'])+' USDT' for r in rows[-10:]) or 'История закрытий пуста.'
                queue.acknowledge(job['id'],text+'\nПроценты в криптоактиве оценены по цене выхода; неполные результаты обозначены как неизвестные.');continue
            day=datetime.fromtimestamp(now,ZoneInfo('Asia/Baku')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            lines=['Торговый отчёт.']
            for title,items in (('Сегодня по Баку',[r for r in rows if day<=r['at_ms']/1000<=now]),('За всё время',rows)):
                with localcontext() as context:
                    context.prec=120
                    gross=None if any(r['gross'] is None for r in items) else sum((Decimal(r['gross']) for r in items),Decimal(0))
                    net=None if any(r['net'] is None for r in items) else sum((Decimal(r['net']) for r in items),Decimal(0))
                    fees=sum((Decimal(r['entry_fee'])+Decimal(r['exit_fee']) for r in items),Decimal(0))
                lines.append(title+': закрытий '+str(len(items))+'; результат до комиссий и процентов '+fmt(gross)+' USDT; комиссии закрытых позиций '+fmt(fees)+' USDT; итог '+fmt(net)+' USDT.')
            lines.append('Проценты в криптоактиве оценены по цене выхода; незавершённая ликвидация даёт неизвестный итог. Общий капитал: /balance_report.')
            queue.acknowledge(job['id'],'\n'.join(lines));continue
        queue.acknowledge(job['id'],'Отчёт пока не подключён.',applied=False)
