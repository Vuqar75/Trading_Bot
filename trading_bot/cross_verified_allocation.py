"""Fixed-price, crash-recoverable initial cross allocation on quiescent copies.

The caller must stop and fence all financial writers and the command receiver
and hold that fence throughout this call. This module never performs networking
or starts/stops services. Unknown or changed state leaves the deployment hold.
"""
import hashlib
import json
import time
from decimal import Decimal
from .cross_rollout import allocate_initial_cross
from .paper_wallet_live_report import live_report


def _prefix(db,limit):
    digest=hashlib.sha256()
    for row in db.execute('SELECT rowid,id,fingerprint FROM ledger_events WHERE rowid<=? ORDER BY rowid',(limit,)):
        digest.update(json.dumps(tuple(row),separators=(',',':')).encode()+b'\n')
    return digest.hexdigest()


def _require_quiescent(check):
    if check() is not True:raise ValueError('Stopped and fenced writers not proven')


def _view(shared,now):
    report=live_report(shared,now=now)
    if report['total'] is None or report['cross'] is None or report['incomplete']:
        raise ValueError('Fixed-price capital is incomplete')
    return {'total':str(report['total']),'spot_realized':None if report['spot_realized'] is None else str(report['spot_realized']),
            'futures_realized':str(report['futures_realized']),'spot_open':str(report['spot_open']),
            'futures_open':str(report['futures_open']),'cross_realized':str(report['cross']['realized']),
            'cross_open':str(report['cross']['gross'])}


def allocate_verified_cross(shared,queue,request_id,*,assert_quiescent,now=None,fault=lambda p:None):
    """Allocate exactly 50 USDT once and retain hold until later safe release."""
    if not isinstance(request_id,str) or not request_id.startswith('cross:initial:'):raise ValueError('Explicit allocation identity required')
    _require_quiescent(assert_quiescent)
    row=shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_allocation_reference'").fetchone()
    reference=json.loads(row[0]) if row else None
    if reference and reference.get('request_id')!=request_id:raise ValueError('Different allocation reference exists')
    already=shared.db.execute('SELECT status FROM transfer_proposals WHERE request_id=?',(request_id,)).fetchone()
    if not already:
        if not shared.deployment_hold():raise ValueError('Deployment hold required')
        stamp=time.time() if now is None else now
        last=shared.db.execute('SELECT COALESCE(MAX(rowid),0) FROM ledger_events').fetchone()[0]
        prefix=_prefix(shared.db,last);view=_view(shared,stamp)
        snapshots={m:shared.snapshot(m) for m in ('spot','futures')}
        quotes={m:shared._get(m,'quote') for m in ('spot','futures')}
        _require_quiescent(assert_quiescent)
        with shared.transaction():
            if not shared.deployment_hold() or _prefix(shared.db,last)!=prefix or shared.db.execute('SELECT COALESCE(MAX(rowid),0) FROM ledger_events').fetchone()[0]!=last:
                raise ValueError('Financial history changed during reference capture')
            if any(shared.snapshot(m)!=snapshots[m] or shared._get(m,'quote')!=quotes[m] for m in snapshots):
                raise ValueError('Market state changed during reference capture')
            reference={'request_id':request_id,'time':stamp,'view':view,'snapshots':snapshots,'quotes':quotes,'last_rowid':last,'prefix':prefix}
            shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('cross_allocation_reference',?)",(json.dumps(reference,sort_keys=True),))
        fault('after_reference_commit')
    elif not reference or already['status']!='DONE':
        raise ValueError('Allocation result or fixed-price reference is uncertain')
    _require_quiescent(assert_quiescent)
    if any(shared._get(m,'quote')!=reference['quotes'][m] for m in ('spot','futures')):
        raise ValueError('Quote snapshot changed; preserve hold and reconcile')
    if _prefix(shared.db,reference['last_rowid'])!=reference['prefix']:
        raise ValueError('Financial history changed before allocation')
    result=allocate_initial_cross(shared,queue,request_id,now=now,keep_hold=True)
    fault('after_allocation_commit')
    _require_quiescent(assert_quiescent)
    if not shared.deployment_hold():raise ValueError('Allocation did not preserve hold')
    current=_view(shared,reference['time'])
    if current!=reference['view']:raise ValueError('Capital or PnL differs at fixed prices')
    post_snapshots={market:shared.snapshot(market) for market in ('spot','futures')}
    for market in ('spot','futures'):
        old=reference['snapshots'][market];new=post_snapshots[market]
        if old['position']!=new['position'] or old['pnl']!=new['pnl'] or old['paused']!=new['paused']:
            raise ValueError('Position or pause changed during allocation')
        expected=Decimal(old['cash'])-(Decimal(50) if market=='futures' else Decimal(0))
        if Decimal(new['cash'])!=expected:raise ValueError('Cash change disagrees with allocation')
    expected_ids={result['event'],'shared:transfer-projection:'+result['token']+':futures'}
    added={row[0] for row in shared.db.execute('SELECT id FROM ledger_events WHERE rowid>?',(reference['last_rowid'],))}
    if added!=expected_ids:raise ValueError('Unexpected financial event during allocation')
    if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
    _require_quiescent(assert_quiescent)
    with shared.transaction():
        added={row[0] for row in shared.db.execute('SELECT id FROM ledger_events WHERE rowid>?',(reference['last_rowid'],))}
        if (not shared.deployment_hold() or _prefix(shared.db,reference['last_rowid'])!=reference['prefix'] or added!=expected_ids
            or any(shared._get(m,'quote')!=reference['quotes'][m] or shared.snapshot(m)!=post_snapshots[m] for m in post_snapshots)
            or not shared.ledger.reconcile()['balanced']):
            raise ValueError('Allocation verification state changed')
        shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('cross_allocation_verified',?)",(json.dumps({'request_id':request_id,'event':result['event'],'capital':current['total']},sort_keys=True),))
    return {'event':result['event'],'replayed':result['replayed'],'capital':current['total']}
