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


def process_margin(queue,receiver,book,shared,*,started=0,clock=time.time,fault=lambda point:None):
    proposals=MarginRepayProposals(book.ledger)
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
            paused=book.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]
            lines=[('Входы на паузе' if paused else 'Paper-входы разрешены')+'; капитал после долгов '+fmt(margin['equity'])+' USDT.']
            for loan in margin['loans']:
                lines.append('Займ '+loan['id']+': основной долг '+fmt(loan['principal'],loan['asset'])+'; проценты '+fmt(loan['interest'],loan['asset'])+' '+loan['asset']+'.')
            if command=='/interest':
                lines.append('Модельные ставки за начатый час (не тариф биржи):')
                for asset in ('USDT','BTC','ETH'):
                    rate=book.db.execute('SELECT rate FROM margin_loan_rates WHERE asset=? AND effective_ms<=? ORDER BY effective_ms DESC LIMIT 1',(asset,int(now*1000))).fetchone()
                    lines.append(asset+': '+fmt(Decimal(rate['rate'])*100 if rate else None)+'% в час.')
                for loan in margin['loans']:
                    last=book.db.execute('SELECT MAX(period) FROM margin_interest_periods WHERE loan_id=?',(loan['id'],)).fetchone()[0]
                    due=(loan['opened_ms']+(0 if last is None else last+1)*3600000)/1000
                    lines.append('Займ '+loan['id']+': следующий период '+when(due)+('. Начисление просрочено.' if due<=now else '.'))
            if margin['due']:lines.append('Начислены не все периоды; оценка неполна.')
            for p in margin['positions']:lines.append(p['symbol']+' '+p['direction']+' '+fmt(p['quantity'],p['symbol'][:-4])+' '+p['symbol'][:-4]+'.')
            if data['incomplete']:lines.append('Неполные данные: '+'; '.join(data['incomplete']))
            queue.acknowledge(job['id'],'\n'.join(lines));continue
        if command=='/signal':
            rows=book.db.execute('SELECT symbol,candle,payload FROM margin_signals ORDER BY symbol').fetchall()
            queue.acknowledge(job['id'],'\n'.join(r['symbol']+': '+json.loads(r['payload'])['action']+'; свеча '+when(r['candle']/1000) for r in rows) or 'Сигнал ещё не рассчитан.');continue
        if command in ('/report','/history'):
            rows=[json.loads(r['details']) for r in book.db.execute("SELECT details FROM ledger_events WHERE kind='FINANCIAL_METADATA' AND id LIKE 'margin:position-close:%' ORDER BY created,id")]
            if command=='/history':
                text='\n'.join(when(r['at_ms']/1000)+' | '+r['symbol']+' '+r['direction']+' | итог '+fmt(r['net'])+' USDT' for r in rows[-10:]) or 'История закрытий пуста.'
                queue.acknowledge(job['id'],text+'\nПроценты в криптоактиве оценены по цене выхода; неполные результаты обозначены как неизвестные.');continue
            day=datetime.fromtimestamp(now,ZoneInfo('Asia/Baku')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            lines=['Spot margin cross • PAPER: торговый отчёт.']
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
