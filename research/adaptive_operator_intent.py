"""Durable, explicitly labelled operator-test intents in the study queue.

This module does not execute orders and cannot itself authorize an entry.
It is not connected to a scheduler, Telegram, or any working account.
"""
import hashlib
import json
import re
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from trading_bot.unified_queue import UnifiedQueue
from trading_bot.futures_store import FuturesStore
from .adaptive_exposure_guard import require_clear
from .adaptive_experiment_fence import authorize, clear
from .adaptive_market_proof import futures_trade_quote, quote as checked_quote
from .usdm_study_sizing import minimum_futures_size

COMMAND = '/research_operator_futures_entry'
EXIT_COMMAND = '/research_operator_futures_exit'


def create_entry(queue, *, test_id, direction, fixed_ms):
    """Create one replay-safe operator intent, without financial writes."""
    if type(queue) is not UnifiedQueue:
        raise ValueError('dedicated_queue_required')
    path = Path(queue.db.execute('PRAGMA database_list').fetchone()[2])
    root = Path(tempfile.gettempdir()).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('research_sandbox_required')
    if not isinstance(test_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{7,63}', test_id):
        raise ValueError('invalid_test_id')
    if direction not in ('LONG', 'SHORT') or type(fixed_ms) is not int or fixed_ms <= 0:
        raise ValueError('invalid_test_intent')
    action = 'operator-test:' + test_id
    update = int(hashlib.sha256(action.encode()).hexdigest()[:15], 16)
    data = {'schema': 1, 'classification': 'execution_test', 'action_id': action,
            'symbol': 'BTCUSDT', 'market': 'futures', 'direction': direction,
            'fixed_ms': fixed_ms, 'leverage': 1, 'max_notional_usdt': '100',
            'max_estimated_risk_usdt': '5'}
    payload = json.dumps(data, sort_keys=True, separators=(',', ':'))
    with queue.transaction():
        previous = queue.db.execute('SELECT fingerprint FROM unified_updates WHERE id=?', (update,)).fetchone()
        if previous:
            if previous[0] != action:
                raise ValueError('operator_update_collision')
            job = queue.db.execute('SELECT id,payload,status FROM unified_jobs WHERE update_id=? AND market=?', (update, 'futures')).fetchone()
            if not job or job['payload'] != payload:
                raise ValueError('operator_action_identity_conflict')
            return {'job_id': job['id'], 'action_id': action, 'status': job['status'], 'replayed': True}
        if queue.db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():
            raise ValueError('queued_action_exists')
        if queue.db.execute("SELECT 1 FROM unified_proposals WHERE status='OPEN' LIMIT 1").fetchone():
            raise ValueError('confirmation_pending')
        queue.db.execute('INSERT INTO unified_updates VALUES(?,?,?,?)', (update, action, COMMAND, 'futures'))
        job = queue.db.execute('INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(?,?,?,?)',
                               (update, 'futures', COMMAND, payload)).lastrowid
        return {'job_id': job, 'action_id': action, 'status': 'PENDING', 'replayed': False}


def execute_entry(store, queue_path, *, job_id, trade, mark, filters, filters_ms,
                  next_funding_ms, clock, fault=lambda stage: None):
    """Execute an approved test entry via the existing isolated paper ledger.

    The committed queue record is the only authority for direction and timing.
    This entry primitive is incomplete without a durable close and recovery
    worker; it is not installed or scheduled by this module.
    """
    if type(store) is not FuturesStore or not store.shared or store.symbol != 'BTCUSDT':
        raise ValueError('shared_isolated_store_required')
    if store.fee != Decimal('10') or store.slip != Decimal('5'):
        raise ValueError('unexpected_execution_costs')
    dbpath = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    queue_path = Path(queue_path)
    root = Path(tempfile.gettempdir()).resolve()
    if dbpath.is_symlink() or queue_path.is_symlink() or not dbpath.resolve().is_relative_to(root) or queue_path.resolve().parent != dbpath.resolve().parent:
        raise ValueError('research_sandbox_required')
    store.db.execute('ATTACH DATABASE ? AS route_queue', (str(queue_path.resolve()),))
    original_transaction, original_margin = store.transaction, store.margin
    action = None
    try:
        row = store.db.execute('SELECT command,payload,status FROM route_queue.unified_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['command'] != COMMAND:
            raise ValueError('operator_job_invalid')
        intent = json.loads(row['payload'])
        action = intent.get('action_id')
        if intent.get('classification') != 'execution_test' or intent.get('direction') not in ('LONG', 'SHORT') or not isinstance(action, str):
            raise ValueError('operator_intent_invalid')
        side = intent['direction']
        saved = store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?', (action,)).fetchone()
        if saved:
            value = json.loads(saved['payload'])
            if saved['event'] != 'OPEN' or value.get('direction') != side:
                raise ValueError('operator_action_identity_conflict')
            if row['status'] != 'DONE':
                return {'status': 'reconciliation_required', 'position': value}
            return {'status': 'filled_existing', 'position': value}
        initial_now = clock()
        evidence = futures_trade_quote(trade, now_ms=initial_now, not_before_ms=intent['fixed_ms'])
        current_mark = checked_quote(mark, market='futures', now_ms=initial_now)
        price = Decimal(evidence['price'])
        fill = price * (1 + (1 if side == 'LONG' else -1) * store.slip / 10000)
        sizing = minimum_futures_size(filters, fill, stop_distance='.02', fee_bps=store.fee,
                                      slippage_bps=store.slip, filter_price=current_mark['price'])
        quantity = Decimal(sizing['quantity'])
        margin = quantity * fill

        def check_clock():
            now = clock()
            if type(intent.get('fixed_ms')) is not int or not 0 <= now-intent['fixed_ms'] <= 120000:
                raise ValueError('operator_intent_stale_or_future')
            futures_trade_quote(trade, now_ms=now, not_before_ms=intent['fixed_ms'])
            m = checked_quote(mark, market='futures', now_ms=now)
            if type(filters_ms) is not int or not 0 <= now-filters_ms <= 120000:
                raise ValueError('stale_filters')
            if type(next_funding_ms) is not int or next_funding_ms <= now:
                raise ValueError('funding_schedule_unavailable')
            if mark.get('next_due_ms') != next_funding_ms:
                raise ValueError('funding_schedule_unavailable')
            current = minimum_futures_size(filters, fill, stop_distance='.02', fee_bps=store.fee,
                                           slippage_bps=store.slip, filter_price=m['price'])
            if Decimal(current['quantity']) != quantity:
                raise ValueError('sizing_changed')
            return now

        @contextmanager
        def guarded_transaction():
            with original_transaction():
                saved = store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?', (action,)).fetchone()
                if saved:
                    value = json.loads(saved['payload'])
                    if saved['event'] != 'OPEN' or value.get('direction') != side:
                        raise ValueError('operator_action_identity_conflict')
                    yield
                    return
                job = store.db.execute('SELECT status,command,payload FROM route_queue.unified_jobs WHERE id=?', (job_id,)).fetchone()
                if not job or job['status'] != 'PENDING' or job['command'] != COMMAND or json.loads(job['payload']) != intent:
                    raise ValueError('operator_job_invalid')
                now = check_clock()
                if store.get('leverage') != 1:
                    raise ValueError('isolated_leverage_not_1x')
                require_clear(store.db, own_job_id=job_id)
                if not authorize(store.db, action, 'futures'):
                    raise ValueError('research_fence_missing')
                try:
                    yield
                    saved = store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='OPEN'", (action,)).fetchone()
                    if saved:
                        pos = json.loads(saved['payload'])
                        store.db.execute('CREATE TABLE IF NOT EXISTS adaptive_funding_schedule(position_id TEXT PRIMARY KEY,next_due_ms INTEGER NOT NULL,observed_ms INTEGER NOT NULL)')
                        store.db.execute('INSERT INTO adaptive_funding_schedule VALUES(?,?,?)', (pos['id'], next_funding_ms, now))
                        result = json.dumps({'classification': 'execution_test', 'position_id': pos['id'],
                                             'trade': evidence, 'mark': current_mark, 'sizing': sizing}, sort_keys=True)
                        store.db.execute('CREATE TABLE IF NOT EXISTS adaptive_operator_receipts(action_id TEXT PRIMARY KEY,position_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)')
                        receipt=json.dumps({'intent':intent,'result':json.loads(result),'next_funding_ms':next_funding_ms},sort_keys=True)
                        store.db.execute('INSERT INTO adaptive_operator_receipts VALUES(?,?,?,?)',(action,pos['id'],'OPEN',receipt))
                        store.db.execute("UPDATE route_queue.unified_jobs SET status='DONE',result=? WHERE id=?", (result, job_id))
                finally:
                    clear(store.db, action, 'futures')

        store.margin = margin
        store.transaction = guarded_transaction
        opened = store.open_position(side, price, initial_now, action, clock_ms=check_clock)
        fault('after_open')
        saved = store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='OPEN'", (action,)).fetchone()
        return {'status': 'filled' if opened else ('filled_existing' if saved else 'not_opened'),
                'position': json.loads(saved['payload']) if saved else None}
    finally:
        store.transaction = original_transaction
        store.margin = original_margin
        store.db.execute('DETACH DATABASE route_queue')


def create_exit(queue, *, test_id, position_id, fixed_ms):
    """Persist a separate, explicitly tagged full-close intent."""
    if type(queue) is not UnifiedQueue:
        raise ValueError('dedicated_queue_required')
    path = Path(queue.db.execute('PRAGMA database_list').fetchone()[2])
    if path.is_symlink() or not path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('research_sandbox_required')
    if not isinstance(test_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{7,63}', test_id):
        raise ValueError('invalid_test_id')
    if not isinstance(position_id, str) or not re.fullmatch(r'[0-9a-f]{32}', position_id) or type(fixed_ms) is not int or fixed_ms <= 0:
        raise ValueError('invalid_test_exit')
    entry = 'operator-test:' + test_id
    action = entry + ':exit'
    update = int(hashlib.sha256(action.encode()).hexdigest()[:15], 16)
    data = {'schema': 1, 'classification': 'execution_test', 'action_id': action,
            'entry_action': entry, 'position_id': position_id, 'fixed_ms': fixed_ms}
    payload = json.dumps(data, sort_keys=True, separators=(',', ':'))
    with queue.transaction():
        previous = queue.db.execute('SELECT fingerprint FROM unified_updates WHERE id=?', (update,)).fetchone()
        if previous:
            if previous[0] != action:
                raise ValueError('operator_update_collision')
            job = queue.db.execute('SELECT id,payload,status FROM unified_jobs WHERE update_id=? AND market=?', (update, 'futures')).fetchone()
            if not job or job['payload'] != payload:
                raise ValueError('operator_action_identity_conflict')
            return {'job_id': job['id'], 'action_id': action, 'status': job['status'], 'replayed': True}
        prior = queue.db.execute("SELECT j.result FROM unified_jobs j JOIN unified_updates u ON j.update_id=u.id WHERE j.command=? AND j.status='DONE' AND u.fingerprint=?", (COMMAND, entry)).fetchone()
        if not prior or not prior['result'] or json.loads(prior['result']).get('position_id') != position_id:
            raise ValueError('operator_entry_not_confirmed')
        if queue.db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():
            raise ValueError('queued_action_exists')
        queue.db.execute('INSERT INTO unified_updates VALUES(?,?,?,?)', (update, action, EXIT_COMMAND, 'futures'))
        job = queue.db.execute('INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(?,?,?,?)',
                               (update, 'futures', EXIT_COMMAND, payload)).lastrowid
        return {'job_id': job, 'action_id': action, 'status': 'PENDING', 'replayed': False}


def execute_exit(store, queue_path, *, job_id, trade, mark, filters, filters_ms, clock, fault=lambda stage: None):
    """Close the identified test position using fresh public evidence."""
    from .adaptive_funding_tracker import cover_through_under_lock
    from .usdm_study_sizing import validate_market_take_bound
    if type(store) is not FuturesStore or not store.shared or store.symbol != 'BTCUSDT':
        raise ValueError('shared_isolated_store_required')
    dbpath = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    queue_path = Path(queue_path)
    if dbpath.is_symlink() or queue_path.is_symlink() or not dbpath.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()) or queue_path.resolve().parent != dbpath.resolve().parent:
        raise ValueError('research_sandbox_required')
    store.db.execute('ATTACH DATABASE ? AS route_queue', (str(queue_path.resolve()),))
    original = store.transaction
    try:
        row = store.db.execute('SELECT command,payload,status FROM route_queue.unified_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['command'] != EXIT_COMMAND:
            raise ValueError('operator_job_invalid')
        intent = json.loads(row['payload'])
        if intent.get('classification') != 'execution_test':
            raise ValueError('operator_intent_invalid')
        action, position_id = intent['action_id'], intent['position_id']
        saved = store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?', (action,)).fetchone()
        if saved:
            value = json.loads(saved['payload'])
            if saved['event'] != 'CLOSE' or value.get('id') != position_id:
                raise ValueError('operator_action_identity_conflict')
            if row['status'] != 'DONE':
                return {'status': 'reconciliation_required', 'position': value}
            return {'status': 'filled_existing', 'position': value}
        now = clock()
        evidence = futures_trade_quote(trade, now_ms=now, not_before_ms=intent['fixed_ms'])
        checked_quote(mark, market='futures', now_ms=now)
        price = Decimal(evidence['price'])

        def check_clock():
            now = clock()
            futures_trade_quote(trade, now_ms=now, not_before_ms=intent['fixed_ms'])
            m = checked_quote(mark, market='futures', now_ms=now)
            if type(filters_ms) is not int or not 0 <= now-filters_ms <= 120000:
                raise ValueError('stale_filters')
            pos = store.get('position')
            if not pos or pos.get('id') != position_id:
                raise ValueError('operator_position_mismatch')
            fill = price * (1 + (1 if pos['direction'] == 'SHORT' else -1) * store.slip / 10000)
            validate_market_take_bound(filters, fill, m['price'])
            return now

        @contextmanager
        def guarded_transaction():
            with original():
                job = store.db.execute('SELECT status,command,payload FROM route_queue.unified_jobs WHERE id=?', (job_id,)).fetchone()
                if not job or job['status'] != 'PENDING' or job['command'] != EXIT_COMMAND or json.loads(job['payload']) != intent:
                    raise ValueError('operator_job_invalid')
                now = check_clock()
                position = store.get('position')
                cover_through_under_lock(store, position, now)
                yield
                saved = store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='CLOSE'", (action,)).fetchone()
                if saved:
                    result = json.dumps({'classification': 'execution_test', 'position_id': position_id,
                                         'trade': evidence, 'mark': mark}, sort_keys=True)
                    store.db.execute('CREATE TABLE IF NOT EXISTS adaptive_operator_receipts(action_id TEXT PRIMARY KEY,position_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)')
                    receipt=json.dumps({'intent':intent,'result':json.loads(result)},sort_keys=True)
                    store.db.execute('INSERT INTO adaptive_operator_receipts VALUES(?,?,?,?)',(action,position_id,'CLOSE',receipt))
                    store.db.execute("UPDATE route_queue.unified_jobs SET status='DONE',result=? WHERE id=?", (result, job_id))

        store.transaction = guarded_transaction
        closed = store.close_position(price, now, action, 'TEST', position_id, clock_ms=check_clock)
        fault('after_close')
        saved = store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='CLOSE'", (action,)).fetchone()
        return {'status': 'filled' if closed else ('filled_existing' if saved else 'not_closed'),
                'position': json.loads(saved['payload']) if saved else None}
    finally:
        store.transaction = original
        store.db.execute('DETACH DATABASE route_queue')


def reconcile_receipt(store, queue_path, *, job_id):
    """Repair only queue acknowledgement from a matching committed receipt.

    An absent or contradictory receipt remains unresolved. No new financial
    event or trade is generated by recovery.
    """
    if type(store) is not FuturesStore or not store.shared or store.symbol != 'BTCUSDT':
        raise ValueError('shared_isolated_store_required')
    dbpath = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    queue_path = Path(queue_path)
    if dbpath.is_symlink() or queue_path.is_symlink() or not dbpath.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()) or queue_path.resolve().parent != dbpath.resolve().parent:
        raise ValueError('research_sandbox_required')
    store.db.execute('ATTACH DATABASE ? AS route_queue', (str(queue_path.resolve()),))
    try:
        with store.transaction():
            job=store.db.execute('SELECT command,payload,status,result FROM route_queue.unified_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['command'] not in (COMMAND,EXIT_COMMAND):raise ValueError('operator_job_invalid')
            intent=json.loads(job['payload']);action=intent.get('action_id');kind='OPEN' if job['command']==COMMAND else 'CLOSE'
            table=store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_operator_receipts'").fetchone()
            receipt=store.db.execute('SELECT position_id,kind,payload FROM adaptive_operator_receipts WHERE action_id=?',(action,)).fetchone() if table else None
            event=store.db.execute('SELECT event,payload FROM futures_events WHERE business_key=?',(action,)).fetchone()
            if not receipt or not event or receipt['kind']!=kind or event['event']!=kind:raise ValueError('receipt_unavailable')
            proof=json.loads(receipt['payload']);financial=json.loads(event['payload'])
            if proof.get('intent')!=intent or financial.get('id')!=receipt['position_id'] or proof.get('result',{}).get('position_id')!=receipt['position_id']:
                raise ValueError('receipt_conflict')
            current=store.get('position')
            if kind=='OPEN' and (not current or current.get('id')!=receipt['position_id']):raise ValueError('position_reconciliation_required')
            if kind=='CLOSE' and current is not None:raise ValueError('position_reconciliation_required')
            result=json.dumps(proof['result'],sort_keys=True)
            if job['status']=='DONE':
                if not job['result'] or json.loads(job['result'])!=proof['result']:raise ValueError('receipt_conflict')
            elif job['status']=='PENDING':
                store.db.execute("UPDATE route_queue.unified_jobs SET status='DONE',result=? WHERE id=?",(result,job_id))
            else:raise ValueError('operator_job_not_recoverable')
            return {'status':'reconciled','position_id':receipt['position_id'],'kind':kind}
    finally:
        store.db.execute('DETACH DATABASE route_queue')
