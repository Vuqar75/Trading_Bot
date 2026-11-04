"""Transactional entry hold and pause restoration for report-only rollout.

The caller must separately prove backup, systemd and live health. No network,
financial posting, database restore or service operation is performed here.
"""
import json
import time
from decimal import localcontext

from .cross_paper_book import CrossPaperBook
from .cross_rollout import _queue_anchor
from .margin_interest_ledger import MarginInterestLedger,HOUR_MS
from .margin_paper_book import MarginPaperBook


def _cross(shared,now):
    book=object.__new__(CrossPaperBook);book.shared=shared;book.db=shared.db;book.ledger=shared.ledger
    book._check()
    row=shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
    if not row or row[0]!=1 or book.runtime_error() or book.funding_conflict():raise ValueError('Cross must remain paused and healthy')
    book._require_coverage(now);book._marks(book.stored_quotes(),now)


def _margin(shared,now):
    book=object.__new__(MarginPaperBook);book.db=shared.db;book.ledger=shared.ledger
    book.loans=object.__new__(MarginInterestLedger);book.loans.db=shared.db;book.loans.ledger=shared.ledger
    book._check()
    error=shared.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
    if not error or json.loads(error[0]) is not None:raise ValueError('Margin requires recovery')
    marks=book._current_marks(book.stored_quotes(),int(now*1000))
    for loan in shared.db.execute("SELECT id,opened_ms FROM margin_loans WHERE status='OPEN'"):
        last=shared.db.execute('SELECT MAX(period) FROM margin_interest_periods WHERE loan_id=?',(loan['id'],)).fetchone()[0]
        if last is None or int(now*1000)<loan['opened_ms'] or last<(int(now*1000)-loan['opened_ms'])//HOUR_MS:
            raise ValueError('Margin interest requires reconciliation')
    with localcontext() as ctx:
        ctx.prec=120
        risk=book._risk(marks)
    if risk['positions'] and risk['equity']<=risk['maintenance']:raise ValueError('Margin protection requires attention')


def _pending(q):
    if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ValueError('Pending commands require processing')


def freeze_report_update(shared,queue,operation,*,now=None,namespace='reports'):
    if namespace not in ('reports','diagnostics','resilience','boundary','finalaudit','helpguide') or not isinstance(operation,str) or not operation.startswith(namespace+':freeze:'):raise ValueError('Explicit operation required')
    freeze_key=namespace+'_update_freeze'
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE');now=time.time() if now is None else now
            old=shared.db.execute('SELECT value FROM ledger_meta WHERE key=?',(freeze_key,)).fetchone()
            if old:
                result=json.loads(old[0])
                if result['operation']!=operation or not shared.deployment_hold():raise ValueError('Existing report hold requires inspection')
                return dict(result,replayed=True)
            if shared.deployment_hold():raise ValueError('Existing deployment hold')
            _pending(q);shared._validate_hold_change(True);_cross(shared,now);_margin(shared,now)
            states={m:shared._assert_consistent(m) for m in ('spot','futures')}
            for market in states:
                if type(states[market]['paused']) is not bool:raise ValueError('Unknown market pause')
                quote=shared._get(market,'quote') or {}
                if shared._get(market,'error') or shared._get(market,'blocked') or not 0<=now-quote.get('time',0)<=120:
                    raise ValueError('Market health requires inspection')
            if not states['spot']['position'].get('fees_known') or shared.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():raise ValueError('Spot requires recovery')
            position=states['futures']['position'];quote=shared._get('futures','quote') or {}
            if position and (type(quote.get('server_ms')) is not int or type(position.get('funding_cursor')) is not int or position['funding_cursor']<quote['server_ms']):raise ValueError('Isolated funding requires reconciliation')
            paused={m:states[m]['paused'] for m in states}
            for market in ('futures_cross','margin_cross'):
                row=shared.db.execute('SELECT paused FROM wallet_state WHERE wallet=?',(market,)).fetchone()
                if not row or row[0] not in (0,1):raise ValueError('Unknown market pause')
                paused[market]=bool(row[0])
            if namespace in ('diagnostics','resilience','boundary','finalaudit','helpguide') and (not paused['futures_cross'] or not paused['margin_cross']):
                raise ValueError('Diagnostics require margin and cross paused')
            if namespace in ('boundary','finalaudit','helpguide') and not paused['futures']:
                raise ValueError('Boundary update requires isolated futures paused')
            offset=int(q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()[0])
            result={'operation':operation,'paused':paused,'queue_offset':offset,'queue_anchor':_queue_anchor(q,offset),'frozen_at':now}
            shared.db.execute('INSERT INTO ledger_meta VALUES(?,?)',(freeze_key,json.dumps(result,sort_keys=True)))
            shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','true')")
            for market in ('spot','futures'):shared._set(market,'paused',True)
            for market in ('futures_cross','margin_cross'):shared.db.execute('UPDATE wallet_state SET paused=1 WHERE wallet=?',(market,))
            return dict(result,replayed=False)
    finally:
        if q.in_transaction:q.rollback()


def release_report_update(shared,queue,operation,*,protection_checked_ms,now=None,namespace='reports'):
    if namespace not in ('reports','diagnostics','resilience','boundary','finalaudit','helpguide') or not isinstance(operation,str) or not operation.startswith(namespace+':release:') or type(protection_checked_ms) is not int:raise ValueError('Explicit release and protection time required')
    freeze_key=namespace+'_update_freeze';release_key=namespace+'_update_release'
    q=queue.db
    try:
        with shared.transaction():
            q.execute('BEGIN IMMEDIATE');now=time.time() if now is None else now
            old=shared.db.execute('SELECT value FROM ledger_meta WHERE key=?',(release_key,)).fetchone()
            if old:
                result=json.loads(old[0])
                if result['operation']!=operation:raise ValueError('Existing release requires inspection')
                return dict(result,replayed=True)
            row=shared.db.execute('SELECT value FROM ledger_meta WHERE key=?',(freeze_key,)).fetchone()
            if not row or not shared.deployment_hold():raise ValueError('Report hold required')
            frozen=json.loads(row[0]);offset=frozen['queue_offset']
            cur=q.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
            if not cur or int(cur[0])<offset or _queue_anchor(q,offset)!=frozen['queue_anchor']:raise ValueError('Queue history changed')
            _pending(q);shared._validate_hold_change(False);_cross(shared,now);_margin(shared,now)
            if not 0<=int(now*1000)-protection_checked_ms<=120000:raise ValueError('Margin protection stale')
            for market in ('futures_cross','margin_cross'):
                row=shared.db.execute('SELECT paused FROM wallet_state WHERE wallet=?',(market,)).fetchone()
                if not row or row[0]!=1:raise ValueError('Market no longer paused')
            position=shared._get('futures','position');quote=shared._get('futures','quote') or {}
            if position and (type(quote.get('server_ms')) is not int or type(position.get('funding_cursor')) is not int or position['funding_cursor']<quote['server_ms']):raise ValueError('Isolated funding requires reconciliation')
            manual=set()
            for update in q.execute("SELECT command,market FROM unified_updates WHERE id>=? AND command IN ('/pause','/pause_all')",(offset,)):
                if update['command']=='/pause_all':manual.update(('spot','futures','futures_cross','margin_cross'))
                else:manual.add(update['market'])
            shared.db.execute("UPDATE ledger_meta SET value='false' WHERE key='deployment_hold'")
            enabled=[]
            for market in ('spot','futures'):
                if frozen['paused'][market] is False and market not in manual:shared._set(market,'paused',False);enabled.append(market)
            if frozen['paused']['margin_cross'] is False and 'margin_cross' not in manual:
                shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'");enabled.append('margin_cross')
            result={'operation':operation,'enabled':enabled,'manual_pauses':sorted(manual),'released_at':now}
            shared.db.execute('INSERT INTO ledger_meta VALUES(?,?)',(release_key,json.dumps(result,sort_keys=True)))
            shared._assert_consistent('spot');shared._assert_consistent('futures');_cross(shared,now);_margin(shared,now)
            return dict(result,replayed=False)
    finally:
        if q.in_transaction:q.rollback()
