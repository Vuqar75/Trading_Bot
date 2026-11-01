"""Offline-tested primitives for an explicitly authorized cross rollout.

This module does not stop services, copy files, reboot, or deploy software.
Call only after coordinated backups and paused health verification.
"""
import time
from decimal import Decimal
from .cross_paper_book import CrossPaperBook
from .paper_wallet_ledger import amount, encode


def allocate_initial_cross(shared, queue, request_id, *, now=None):
    """Release deployment hold and transfer exactly 50 paper USDT atomically.

    The queue is locked until the financial commit. A completed request is
    idempotent even after a crash; no other source or amount is permitted.
    """
    if not isinstance(request_id,str) or not request_id.startswith('cross:initial:'):
        raise ValueError('Explicit rollout identity required')
    now=time.time() if now is None else now
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE')
            if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():
                raise ValueError('Pending commands require processing')
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            before={m:shared._assert_consistent(m) for m in ('spot','futures')}
            old=shared.db.execute('SELECT * FROM transfer_proposals WHERE request_id=?',(request_id,)).fetchone()
            if old and old['status']=='DONE':
                if old['source']!='futures_isolated' or old['destination']!='futures_cross' or amount(old['quantity'])!=50:
                    raise ValueError('Conflicting initial allocation')
                return {'token':old['id'],'event':old['event_id'],'replayed':True}
            if old:raise ValueError('Initial allocation is incomplete; inspect before retry')
            shared._validate_hold_change(False)
            if not shared.deployment_hold():raise ValueError('Deployment hold required')
            isolated=before['futures']['position'];quote=shared._get('futures','quote') or {}
            if isolated is not None and (type(quote.get('server_ms')) is not int or type(isolated.get('funding_cursor')) is not int or isolated['funding_cursor']<quote['server_ms']):
                raise ValueError('Isolated funding is not fully reconciled')
            cross=object.__new__(CrossPaperBook);cross.shared=shared;cross.db=shared.db;cross.ledger=shared.ledger
            cross._check()
            if not shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]:raise ValueError('Cross must be paused')
            if cross._positions() or any(shared.ledger.balance('futures_cross:'+a) for a in ('free','reserved','margin','funding','liability')):
                raise ValueError('Cross already has positions or funds')
            if shared.db.execute("SELECT 1 FROM ledger_events WHERE kind LIKE 'CROSS_%' LIMIT 1").fetchone():raise ValueError('Cross financial history already exists')
            if cross.runtime_error():raise ValueError('Cross requires recovery')
            cross._current_marks(cross.stored_quotes(),now)
            # Hold release and allocation commit together; queue cannot accept
            # another command until the financial transaction is committed.
            shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','false')")
            shared._sync_pause_and_validate('futures_isolated','futures_cross','50',now)
            proposal=shared.ledger.propose_transfer(request_id,'futures_isolated','futures_cross','50',now=now)
            event=shared.ledger.confirm_transfer(proposal['id'],now=now)
            shared._set('futures','cash',encode(amount(shared._get('futures','cash'))-Decimal(50)))
            for market in ('spot','futures'):
                shared._record_projection(market,before[market],shared.snapshot(market),'shared:transfer-projection:'+proposal['id']+':'+market,{'transfer':proposal['id']})
            shared._assert_consistent('spot');shared._assert_consistent('futures');cross._check()
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            return {'token':proposal['id'],'event':event,'replayed':False}
    finally:
        if q.in_transaction:q.rollback()
