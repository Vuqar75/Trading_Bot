"""Paper-only wallet commands, dispatched from the durable unified inbox."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .paper_wallet_live_report import live_report,render_live_report


def process_wallet(queue,shared,*,started=0,fault=lambda p:None):
    for job in queue.pending('wallet')[:20]:
        parts=job['command'].split();cmd=parts[0];payload=json.loads(job['payload'])
        try:
            if cmd=='/transfer':
                if payload.get('received_at',0)<started or time.time()>=payload.get('expires',0):
                    raise ValueError('Запрос устарел после перезапуска или ожидания')
                _,source,destination,value=parts
                p=shared.propose_transfer('telegram:'+str(job['update_id']),source,destination,value)
                deadline=datetime.fromtimestamp(p['expires'],ZoneInfo('Asia/Baku')).strftime('%H:%M:%S Баку')
                result=(f"Перевод {p['quantity']} USDT: {p['source']} → {p['destination']}. "
                        f"После перевода: {p['source_after']} и {p['destination_after']} USDT. "
                        f"Подтвердите /confirm {p['id']} или отмените /cancel {p['id']} до {deadline}.")
            elif cmd=='/confirm_transfer':
                token=payload['token']
                prior=shared.db.execute('SELECT status FROM transfer_proposals WHERE id=?',(token,)).fetchone()
                if payload.get('received_at',0)<started and (not prior or prior[0]!='DONE'):
                    raise ValueError('Подтверждение принято до перезапуска; отправьте новое')
                shared.confirm_transfer(token)
                result='Виртуальный перевод подтверждён и учтён один раз.'
            elif cmd=='/cancel_transfer':
                shared.cancel_transfer(payload['token']);result='Предложение перевода отменено.'
            elif cmd in ('/wallets','/wallet','/balance_report'):
                if cmd=='/wallet' and parts[1] not in ('reserve','spot','futures_isolated','futures_cross'):
                    raise ValueError('Доступны reserve, spot, futures_isolated и futures_cross')
                period=parts[1] if cmd=='/balance_report' and len(parts)==2 else 'all'
                data=live_report(shared,period=period)
                if cmd=='/wallet':
                    w=parts[1]
                    if w not in data['wallets']:raise ValueError('Кошелёк пока недоступен')
                    v=data['wallets'][w]
                    result=f"{w}: свободно {v['free']} USDT; резерв {v['reserved']} USDT. Сверка пройдена."
                    if w=='spot':result+=f" Spot актив: {data['btc']} BTC."
                    if w=='futures_isolated':result+=f" Маржа {data['margin']} USDT; funding {data['funding_open']} USDT."
                    if w=='futures_cross':
                        cross=data['cross'];available=cross['available'] if cross['available'] is not None else 'неизвестно'
                        result+=f" Общая маржа {cross['margin']} USDT; доступно для перевода {available}; позиций {len(cross['positions'])}."
                else:result=render_live_report(data)
            elif cmd=='/transfers':
                with shared.transaction():
                    rows=shared.db.execute("SELECT created,details FROM ledger_events WHERE kind='TRANSFER' ORDER BY created DESC,id DESC LIMIT 10").fetchall()
                    items=[(r['created'],json.loads(r['details'])) for r in rows]
                result='\n'.join(datetime.fromtimestamp(ts,ZoneInfo('Asia/Baku')).strftime('%Y-%m-%d %H:%M:%S Баку')+' | '+p['source']+' → '+p['destination']+' | '+p['quantity']+' USDT' for ts,p in items) or 'Переводов пока нет.'
            else:raise ValueError('Неизвестная команда кошельков')
            fault('after_destination_commit')
            queue.acknowledge(job['id'],result)
        except ValueError as exc:
            # Validation/reconciliation rejects the operation permanently; storage
            # and unexpected internal errors propagate and retain the queue item.
            message=str(exc)
            translations={'Trading wallet must be paused':'нужна пауза затронутых торговых кошельков',
                          'Insufficient confirmed free funds':'недостаточно подтверждённых свободных средств',
                          'Transfer confirmation expired or canceled':'подтверждение истекло или отменено',
                          'Unknown transfer confirmation':'подтверждение перевода не найдено',
                          'Unsupported transfer':'кошелёк или направление недоступны',
                          'Source requires recovery before import':'требуется сверка источника',
                          'Spot requires recovery or fee valuation':'Spot требует сверки или оценки комиссии',
                          'Spot order unresolved':'Spot имеет незавершённый ордер',
                          'Futures requires recovery':'Futures требует сверки',
                          'Journal integrity mismatch':'обнаружено расхождение журнала; нужна проверка',
                          'Unsupported report period':'используйте today или all'}
            queue.acknowledge(job['id'],'Операция отклонена: '+translations.get(message, message if any(ord(c)>127 for c in message) else 'проверьте параметры, состояние кошельков и сверку.'),applied=False)
