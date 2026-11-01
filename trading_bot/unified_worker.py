"""Process one market's durable commands outside all network operations."""
import json
import time
from decimal import Decimal
from .telegram_reports import when


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
            if qty <= 0 or not pos.get('id') or store.get('blocked') or store.get('error') or store.get('close_request') or (market == 'spot' and store.pending()):
                queue.acknowledge(job['id'], 'Закрытие недоступно: нет подтверждённой позиции или требуется проверка.', applied=False)
            else:
                queue.add_proposal(job['update_id'], market, pos['id'], str(qty))
            continue
        if action == '/brief_status':
            quote = store.get('quote') or {}
            stamp = quote.get('time')
            pos = store.get('position')
            msg = ('входы на паузе' if store.get('paused') else 'входы разрешены')
            msg += '; позиция: '+(str(pos.get('direction', 'открыта')) if isinstance(pos, dict) and Decimal(str(pos.get('quantity', '0'))) > 0 else 'нет')
            msg += '; обновление: '+(when(stamp) if stamp else 'нет данных')
            if stamp and time.time()-stamp > 120:
                msg += ' (устарело)'
            if store.get('error') or store.get('blocked'):
                msg += '; есть ошибка или блокировка'
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
        queue.acknowledge(job['id'], 'Команда обработана; подробный ответ отправляется отдельно.')
