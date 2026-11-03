"""Process one market's durable commands outside all network operations."""
import json
import time
from decimal import Decimal
from .telegram_reports import when
from .status_summary import brief_status


def process_market(queue, receiver, store, market, *, started=0, fault=lambda point: None):
    for job in queue.pending(market)[:20]:
        action = job['command']
        payload = json.loads(job['payload'])
        if action in ('/resume', '/confirmed_close') or action.startswith(('/timeframe ', '/leverage ')):
            if payload.get('received_at', 0) < started:
                queue.acknowledge(job['id'], 'Команда принята до перезапуска; отправьте новую.', applied=False)
                continue
        if action == '/close':
            pos = store.get('position')
            try:
                qty = Decimal(str(pos['quantity'])) if isinstance(pos, dict) else Decimal(0)
            except (ValueError, ArithmeticError, KeyError):
                qty = Decimal(0)
            if (qty <= 0 or not pos.get('id') or pos.get('id') != payload.get('position') or str(qty) != payload.get('quantity') or time.time() >= payload.get('expires', 0) or store.get('blocked') or store.get('error') or store.get('close_request') or (market == 'spot' and store.pending())):
                queue.acknowledge(job['id'], 'Закрытие недоступно: нет подтверждённой позиции или требуется проверка.', applied=False)
            else:
                queue.add_proposal(job['update_id'], market, pos['id'], str(qty), ttl=payload['expires']-time.time())
            continue
        if action == '/brief_status':
            quote = store.get('quote') or {}
            stamp = quote.get('time')
            pos = store.get('position')
            count=int(isinstance(pos,dict) and Decimal(str(pos.get('quantity','0')))>0)
            msg=brief_status(paused=bool(store.get('paused')),positions=count,stamp=stamp,now=time.time(),
                             problem=bool(store.get('error') or store.get('blocked')))
            queue.acknowledge(job['id'], msg)
            continue
        if action in ('/pause', '/resume', '/confirmed_close'):
            result = receiver.apply(job['update_id'], action, payload)
            fault('after_destination_commit')
            applied = action != '/pause' or bool(store.get('paused'))
            queue.acknowledge(job['id'], result, applied=applied)
            continue
        if payload.get('expires') is not None and time.time() >= payload['expires']:
            queue.acknowledge(job['id'], 'Команда устарела до обработки.', applied=False)
            continue
        store.command(job['update_id'], True, action, routed=True)
        fault('after_destination_commit')
        # These commands write their answer durably to the market outbox in
        # store.command; acknowledging the queue must not send a second reply.
        informational=action in ('/status','/signal','/history','/report','/timeframe','/leverage')
        queue.acknowledge(job['id'], 'Ответ сохранён в журнале рынка.', notify=not informational)
