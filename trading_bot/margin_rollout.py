"""Transactional pause, one allocation and release for margin paper cutover.

No service control, network access or filesystem replacement occurs here.
The caller must independently verify backups, service health and process fences.
"""
import json
import time
from decimal import Decimal, localcontext

from .cross_paper_book import CrossPaperBook
from .cross_rollout import _queue_anchor
from .margin_interest_ledger import MarginInterestLedger
from .margin_paper_book import MarginPaperBook
from .paper_wallet_ledger import amount,encode


def _cross(shared):
    book=object.__new__(CrossPaperBook);book.shared=shared;book.db=shared.db;book.ledger=shared.ledger
    return book


def _margin(shared):
    from .paper_pair_runtime_offline import margin_book_view
    return margin_book_view(shared.db,shared.ledger)


def _pending(q):
    if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ValueError('Pending commands require processing')


def _verified_allocation(shared,saved):
    row=shared.db.execute('SELECT * FROM transfer_proposals WHERE id=?',(saved.get('token'),)).fetchone()
    event=shared.db.execute('SELECT kind,details FROM ledger_events WHERE id=?',(saved.get('event'),)).fetchone()
    details=json.loads(event['details']) if event else {}
    if (not row or row['status']!='DONE' or row['request_id']!=saved.get('request_id') or row['event_id']!=saved.get('event') or
        row['source']!='futures_isolated' or row['destination']!='margin_cross' or amount(row['quantity'])!=50 or
        not event or event['kind']!='TRANSFER' or details.get('source')!='futures_isolated' or details.get('destination')!='margin_cross' or amount(details.get('quantity','NaN'))!=50):
        raise ValueError('Margin allocation mismatch')
    rows=shared.db.execute('SELECT account,asset,amount FROM ledger_postings WHERE event_id=? ORDER BY ordinal',(saved['event'],)).fetchall()
    if [(r['account'],r['asset'],amount(r['amount'])) for r in rows]!=[('futures_isolated:free','USDT',Decimal(-50)),('margin_cross:free','USDT',Decimal(50))] or not shared.ledger.reconcile()['balanced']:
        raise ValueError('Margin allocation postings disagree')


def _cross_paused(shared,now):
    book=_cross(shared);book._check()
    row=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
    if not row or row[0]!=1 or book.runtime_error() or book.funding_conflict():raise ValueError('Cross must be paused and healthy')
    book._require_coverage(now);book._marks(book.stored_quotes(),now,book._symbols())


def freeze_margin_cutover(shared,queue,request_id,*,now=None):
    if not isinstance(request_id,str) or not request_id.startswith('margin:freeze:'):raise ValueError('Explicit freeze identity required')
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE');now=time.time() if now is None else now
            previous=shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_cutover_freeze'").fetchone()
            if previous:
                saved=json.loads(previous[0])
                if saved['request_id']!=request_id or not shared.deployment_hold():raise ValueError('Existing margin freeze requires inspection')
                return dict(saved,replayed=True)
            if shared.deployment_hold():raise ValueError('Existing deployment hold')
            _pending(q);shared._validate_hold_change(True);_cross_paused(shared,now)
            states={m:shared._assert_consistent(m) for m in ('spot','futures')}
            for market in states:
                quote=shared._get(market,'quote') or {}
                if type(states[market]['paused']) is not bool or shared._get(market,'error') or shared._get(market,'blocked') or not 0<=now-quote.get('time',0)<=120:
                    raise ValueError('Market health requires inspection')
            if not states['spot']['position'].get('fees_known') or shared.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():raise ValueError('Spot requires recovery')
            pos=states['futures']['position'];quote=shared._get('futures','quote') or {}
            if pos and (type(quote.get('server_ms')) is not int or type(pos.get('funding_cursor')) is not int or pos['funding_cursor']<quote['server_ms']):raise ValueError('Isolated funding requires reconciliation')
            offset=int(q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()[0])
            saved={'request_id':request_id,'paused':{m:states[m]['paused'] for m in states},'queue_offset':offset,'queue_anchor':_queue_anchor(q,offset),'frozen_at':now}
            shared.db.execute("INSERT INTO ledger_meta VALUES('margin_cutover_freeze',?)",(json.dumps(saved,sort_keys=True),))
            shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','true')")
            for m in states:shared._set(m,'paused',True)
            return dict(saved,replayed=False)
    finally:
        if q.in_transaction:q.rollback()


def allocate_margin_once(shared,queue,request_id,*,now=None):
    if not isinstance(request_id,str) or not request_id.startswith('margin:initial:'):raise ValueError('Explicit allocation identity required')
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE');now=time.time() if now is None else now
            old=shared.db.execute('SELECT * FROM transfer_proposals WHERE request_id=?',(request_id,)).fetchone()
            if old:
                marker=shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_cutover_allocation'").fetchone()
                saved=json.loads(marker[0]) if marker else {}
                if old['status']!='DONE' or old['source']!='futures_isolated' or old['destination']!='margin_cross' or amount(old['quantity'])!=50 or saved.get('event')!=old['event_id']:
                    raise ValueError('Existing margin allocation requires inspection')
                _verified_allocation(shared,saved)
                return dict(saved,replayed=True)
            _pending(q)
            if not shared.deployment_hold() or not shared.db.execute("SELECT 1 FROM ledger_meta WHERE key='margin_cutover_freeze'").fetchone():raise ValueError('Margin freeze required')
            shared._validate_hold_change(False);_cross_paused(shared,now)
            before={m:shared._assert_consistent(m) for m in ('spot','futures')}
            p=before['futures']['position'];quote=shared._get('futures','quote') or {}
            if p and (type(quote.get('server_ms')) is not int or type(p.get('funding_cursor')) is not int or p['funding_cursor']<quote['server_ms']):raise ValueError('Isolated funding requires reconciliation')
            margin=_margin(shared);margin._check()
            if margin._positions() or shared.db.execute("SELECT 1 FROM margin_loans LIMIT 1").fetchone() or any(shared.ledger.balance('margin_cross:'+bucket,asset) for bucket in ('free','reserved','principal','interest') for asset in ('USDT','BTC','ETH')):
                raise ValueError('Margin already has funds or history')
            shared.db.execute("UPDATE ledger_meta SET value='false' WHERE key='deployment_hold'")
            shared._sync_pause_and_validate('futures_isolated','margin_cross','50',now)
            proposal=shared.ledger.propose_transfer(request_id,'futures_isolated','margin_cross','50',now=now)
            event=shared.ledger.confirm_transfer(proposal['id'],now=now)
            with localcontext() as context:
                context.prec=120
                shared._set('futures','cash',encode(amount(shared._get('futures','cash'))-Decimal(50)))
            for m in ('spot','futures'):
                shared._record_projection(m,before[m],shared.snapshot(m),'shared:transfer-projection:'+proposal['id']+':'+m,{'transfer':proposal['id']})
            shared.db.execute("UPDATE ledger_meta SET value='true' WHERE key='deployment_hold'")
            saved={'request_id':request_id,'token':proposal['id'],'event':event,'at':now}
            shared.db.execute("INSERT INTO ledger_meta VALUES('margin_cutover_allocation',?)",(json.dumps(saved,sort_keys=True),))
            _verified_allocation(shared,saved)
            shared._assert_consistent('spot');shared._assert_consistent('futures');margin._check()
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal mismatch')
            return dict(saved,replayed=False)
    finally:
        if q.in_transaction:q.rollback()


def release_margin_cutover(shared,queue,request_id,*,protection_checked_ms,now=None):
    if not isinstance(request_id,str) or not request_id.startswith('margin:release:') or type(protection_checked_ms) is not int:raise ValueError('Explicit release and protection time required')
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE');now=time.time() if now is None else now
            previous=shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_cutover_release'").fetchone()
            if previous:
                saved=json.loads(previous[0])
                if saved['request_id']!=request_id:raise ValueError('Existing release requires inspection')
                return dict(saved,replayed=True)
            frozen=shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_cutover_freeze'").fetchone()
            allocation=shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_cutover_allocation'").fetchone()
            if not frozen or not allocation or not shared.deployment_hold():raise ValueError('Freeze and allocation required')
            frozen=json.loads(frozen[0]);allocation=json.loads(allocation[0]);offset=frozen['queue_offset']
            current=q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
            if not current or int(current[0])<offset or _queue_anchor(q,offset)!=frozen['queue_anchor']:raise ValueError('Queue history changed')
            _pending(q);shared._validate_hold_change(False);_cross_paused(shared,now)
            _verified_allocation(shared,allocation)
            position=shared._get('futures','position');quote=shared._get('futures','quote') or {}
            if position and (type(quote.get('server_ms')) is not int or type(position.get('funding_cursor')) is not int or position['funding_cursor']<quote['server_ms']):raise ValueError('Isolated funding requires reconciliation')
            margin=_margin(shared);margin._check();now_ms=int(now*1000)
            if not 0<=now_ms-protection_checked_ms<=120000:raise ValueError('Margin protection is stale')
            state=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()
            error=shared.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
            if not state or state[0]!=1 or not error or json.loads(error[0]) is not None:raise ValueError('Margin requires recovery')
            marks=margin._current_marks(margin.stored_quotes(),now_ms);margin._accrue_all(now_ms);margin._check()
            risk=margin._risk(marks)
            if risk['equity']<=risk['maintenance']:raise ValueError('Margin collateral insufficient')
            manual=set()
            for row in q.execute("SELECT command,market FROM unified_updates WHERE id>=? AND command IN ('/pause','/pause_all')",(offset,)):
                if row['command']=='/pause_all':manual.update(('spot','futures','futures_cross','margin_cross'))
                else:manual.add(row['market'])
            shared.db.execute("UPDATE ledger_meta SET value='false' WHERE key='deployment_hold'")
            enabled=[]
            for market in ('spot','futures'):
                if frozen['paused'][market] is False and market not in manual:shared._set(market,'paused',False);enabled.append(market)
            if 'margin_cross' not in manual:
                shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'");enabled.append('margin_cross')
            saved={'request_id':request_id,'enabled':enabled,'manual_pauses':sorted(manual),'released_at':now}
            shared.db.execute("INSERT INTO ledger_meta VALUES('margin_cutover_release',?)",(json.dumps(saved,sort_keys=True),))
            shared._assert_consistent('spot');shared._assert_consistent('futures');margin._check();_cross_paused(shared,now)
            return dict(saved,replayed=False)
    finally:
        if q.in_transaction:q.rollback()
