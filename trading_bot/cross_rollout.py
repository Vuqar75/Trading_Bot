"""Offline-tested primitives for an explicitly authorized cross rollout.

This module does not stop services, copy files, reboot, or deploy software.
Call only after coordinated backups and paused health verification.
"""
import json
import hashlib
import time
from decimal import Decimal
from .cross_paper_book import CrossPaperBook
from .paper_wallet_ledger import amount, encode


def _queue_anchor(db, offset):
    digest=hashlib.sha256();count=0
    for row in db.execute('SELECT id,fingerprint,command,market FROM unified_updates WHERE id<? ORDER BY id',(offset,)):
        digest.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode('utf-8')+b'\n');count+=1
    return {'count':count,'sha256':digest.hexdigest()}


def freeze_entries(shared, queue, request_id, *, now=None):
    """Persist original pause states and hold both markets in one transaction.

    The queue lock is held through financial commit. Repeating the same request
    recovers the durable record after a crash; it never changes the prior pause
    states. This is an offline primitive, not an installer or a process fence.
    """
    if not isinstance(request_id,str) or not request_id.startswith('cross:freeze:'):
        raise ValueError('Explicit freeze identity required')
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE')
            now=time.time() if now is None else now
            row=shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_freeze'").fetchone()
            if row:
                saved=json.loads(row[0])
                if saved.get('request_id')!=request_id or not shared.deployment_hold():
                    raise ValueError('Existing cutover requires inspection')
                if any(shared._get(m,'paused') is not True for m in ('spot','futures')):
                    raise ValueError('Frozen market pause changed; inspect')
                return dict(saved,replayed=True)
            if shared.deployment_hold():raise ValueError('Existing deployment hold')
            if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():
                raise ValueError('Pending commands require processing')
            shared._validate_hold_change(True)
            before={m:shared._assert_consistent(m) for m in ('spot','futures')}
            for market in ('spot','futures'):
                quote=shared._get(market,'quote') or {}
                if type(before[market]['paused']) is not bool or shared._get(market,'error') or shared._get(market,'blocked'):
                    raise ValueError('Market state requires recovery')
                stamp=quote.get('time')
                if not isinstance(stamp,(float,int)) or not 0<=now-stamp<=120:
                    raise ValueError('Market quote stale')
            if not before['spot']['position'].get('fees_known') or shared.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
                raise ValueError('Spot requires recovery or fee review')
            position=before['futures']['position'];fq=shared._get('futures','quote') or {}
            if position is not None and (type(fq.get('server_ms')) is not int or type(position.get('funding_cursor')) is not int or position['funding_cursor']<fq['server_ms']):
                raise ValueError('Isolated funding is not fully reconciled')
            offset=q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
            if not offset:raise ValueError('Queue offset unavailable')
            offset=int(offset[0])
            saved={'request_id':request_id,'paused':{m:before[m]['paused'] for m in before},'queue_offset':offset,'queue_anchor':_queue_anchor(q,offset),'frozen_at':now}
            shared.db.execute("INSERT INTO ledger_meta VALUES('cross_cutover_freeze',?)",(json.dumps(saved,sort_keys=True),))
            shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','true')")
            for market in ('spot','futures'):shared._set(market,'paused',True)
            shared._assert_consistent('spot');shared._assert_consistent('futures')
            return dict(saved,replayed=False)
    finally:
        if q.in_transaction:q.rollback()


def allocate_initial_cross(shared, queue, request_id, *, now=None):
    """Release deployment hold and transfer exactly 50 paper USDT atomically.

    The queue is locked until the financial commit. A completed request is
    idempotent even after a crash; no other source or amount is permitted.
    """
    if not isinstance(request_id,str) or not request_id.startswith('cross:initial:'):
        raise ValueError('Explicit rollout identity required')
    live_clock=now is None
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE')
            now=time.time() if now is None else now
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
            if live_clock:now=time.time()
            cross._current_marks(cross.stored_quotes(),now)
            # Hold release and allocation commit together; queue cannot accept
            # another command until the financial transaction is committed.
            shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','false')")
            shared._sync_pause_and_validate('futures_isolated','futures_cross','50',now)
            proposal=shared.ledger.propose_transfer(request_id,'futures_isolated','futures_cross','50',now=now)
            event=shared.ledger.confirm_transfer(proposal['id'],now=now)
            shared.db.execute("INSERT INTO ledger_meta VALUES('cross_cutover_allocation',?)",(json.dumps({'request_id':request_id,'token':proposal['id'],'event':event},sort_keys=True),))
            shared._set('futures','cash',encode(amount(shared._get('futures','cash'))-Decimal(50)))
            for market in ('spot','futures'):
                shared._record_projection(market,before[market],shared.snapshot(market),'shared:transfer-projection:'+proposal['id']+':'+market,{'transfer':proposal['id']})
            shared._assert_consistent('spot');shared._assert_consistent('futures');cross._check()
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            return {'token':proposal['id'],'event':event,'replayed':False}
    finally:
        if q.in_transaction:q.rollback()


def release_entries_after_cutover(shared, queue, request_id, *, now=None):
    """Restore eligible entries once, respecting later durable pause commands.

    Call only after independent backup, reboot, health and single-writer checks.
    This function cannot establish those external conditions by itself.
    """
    if not isinstance(request_id,str) or not request_id.startswith('cross:release:'):
        raise ValueError('Explicit release identity required')
    live_clock=now is None
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE')
            now=time.time() if now is None else now
            row=shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_release'").fetchone()
            if row:
                saved=json.loads(row[0])
                if saved.get('request_id')!=request_id:raise ValueError('Existing release requires inspection')
                return dict(saved,replayed=True)
            frozen=shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_freeze'").fetchone()
            if not frozen:raise ValueError('Cutover freeze record required')
            freeze=json.loads(frozen[0])
            offset=q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
            if not offset or int(offset[0])<freeze['queue_offset'] or _queue_anchor(q,freeze['queue_offset'])!=freeze.get('queue_anchor'):
                raise ValueError('Queue history changed since freeze')
            allocation=shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_allocation'").fetchone()
            if not allocation:raise ValueError('Confirmed initial allocation required')
            allocation=json.loads(allocation[0])
            transfer=shared.db.execute('SELECT * FROM transfer_proposals WHERE id=?',(allocation['token'],)).fetchone()
            if not transfer or transfer['status']!='DONE' or transfer['event_id']!=allocation['event'] or transfer['request_id']!=allocation['request_id'] or transfer['source']!='futures_isolated' or transfer['destination']!='futures_cross' or amount(transfer['quantity'])!=50:
                raise ValueError('Initial allocation disagrees with ledger')
            event=shared.db.execute('SELECT kind,details FROM ledger_events WHERE id=?',(allocation['event'],)).fetchone()
            details=json.loads(event['details']) if event else {}
            if not event or event['kind']!='TRANSFER' or details.get('source')!='futures_isolated' or details.get('destination')!='futures_cross' or amount(details.get('quantity','NaN'))!=50:
                raise ValueError('Initial allocation has no matching transfer event')
            postings=shared.db.execute('SELECT account,asset,amount FROM ledger_postings WHERE event_id=? ORDER BY ordinal',(allocation['event'],)).fetchall()
            if [(p['account'],p['asset'],amount(p['amount'])) for p in postings] != [('futures_isolated:free','USDT',Decimal(-50)),('futures_cross:free','USDT',Decimal(50))]:
                raise ValueError('Initial allocation postings disagree')
            if shared.deployment_hold():raise ValueError('Deployment hold is active')
            if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():
                raise ValueError('Pending commands require processing')
            shared._validate_hold_change(False)
            cross=object.__new__(CrossPaperBook);cross.shared=shared;cross.db=shared.db;cross.ledger=shared.ledger
            cross._check()
            state=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
            if not state or state[0]!=1 or cross.runtime_error() or shared.ledger.balance('futures_cross:liability')<0:
                raise ValueError('Cross requires paused healthy state')
            if live_clock:now=time.time()
            cross._require_coverage(now);marks=cross._marks(cross.stored_quotes(),now)
            if cross._risk(marks)['buffer']<=0:raise ValueError('Cross margin buffer exhausted')
            pauses=q.execute("SELECT command,market FROM unified_updates WHERE id>=? AND command IN ('/pause','/pause_all')",(freeze['queue_offset'],)).fetchall()
            manual={m for command,market in pauses for m in (('spot','futures','futures_cross') if command=='/pause_all' else (market,))}
            enabled=[]
            for market in ('spot','futures'):
                if freeze['paused'][market] is False and market not in manual:
                    shared._set(market,'paused',False);enabled.append(market)
            if 'futures_cross' not in manual:
                shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='futures_cross'");enabled.append('futures_cross')
            saved={'request_id':request_id,'enabled':enabled,'manual_pauses':sorted(manual),'released_at':now}
            shared.db.execute("INSERT INTO ledger_meta VALUES('cross_cutover_release',?)",(json.dumps(saved,sort_keys=True),))
            shared._assert_consistent('spot');shared._assert_consistent('futures');cross._check()
            return dict(saved,replayed=False)
    finally:
        if q.in_transaction:q.rollback()
