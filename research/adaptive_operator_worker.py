"""Uninstalled operator-test worker for the existing isolated study account.

This worker is separate from strategy decisions and has no Telegram or order
API. The caller must hold the study writer/cycle locks and supervise recovery.
"""
import json
import os
import time
from pathlib import Path
from trading_bot.futures_store import FuturesStore
from trading_bot.unified_queue import UnifiedQueue
from . import adaptive_public_feed as public_feed
from .adaptive_funding_tracker import advance
from .adaptive_operator_intent import create_entry, create_exit, execute_entry, execute_exit, reconcile_receipt


def _job(queue, action):
    row=queue.db.execute('SELECT j.id,j.status,j.payload,j.result FROM unified_updates u JOIN unified_jobs j ON j.update_id=u.id WHERE u.fingerprint=?',(action,)).fetchone()
    return dict(row) if row else None


def step(path, *, test_id, direction, clock, provider=public_feed, fault=lambda stage:None):
    """Advance one authorised test; no strategy signal is generated.

    Call only while holding the study's writer and cycle locks. Each invocation
    may leave a real *virtual* position open and must therefore be supervised.
    """
    if direction not in ('LONG','SHORT'):raise ValueError('invalid_test_direction')
    path=Path(path)
    queue=UnifiedQueue(path/'queue.db');store=None
    try:
        store=FuturesStore(path/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
        action='operator-test:'+test_id;exit_action=action+':exit'
        entry=_job(queue,action);exit_job=_job(queue,exit_action);position=store.get('position')
        if exit_job and exit_job['status']=='DONE':
            if position is not None:raise ValueError('completed_test_has_position')
            result=store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='CLOSE'",(exit_action,)).fetchone()
            if not result:raise ValueError('completed_test_missing_close')
            return {'status':'COMPLETE','close':json.loads(result['payload'])}
        if position:
            event=store.db.execute("SELECT payload FROM futures_events WHERE business_key=? AND event='OPEN'",(action,)).fetchone()
            if not entry or not event or json.loads(event['payload']).get('id')!=position.get('id') or position.get('direction')!=direction:
                raise ValueError('unrelated_position_requires_review')
            if entry['status']=='PENDING':
                reconcile_receipt(store,path/'queue.db',job_id=entry['id']);entry=_job(queue,action)
            if entry['status']!='DONE':raise ValueError('entry_reconciliation_required')
            if not exit_job:
                create_exit(queue,test_id=test_id,position_id=position['id'],fixed_ms=clock())
                fault('after_exit_intent');exit_job=_job(queue,exit_action)
            if exit_job['status']!='PENDING':raise ValueError('exit_reconciliation_required')
            intent=json.loads(exit_job['payload'])
            mark=provider.futures_quote()
            raw={'symbol':'BTCUSDT','time':mark['event_ms'],'markPrice':mark['price'],'nextFundingTime':mark['next_due_ms']}
            schedule=store.db.execute('SELECT next_due_ms FROM adaptive_funding_schedule WHERE position_id=?',(position['id'],)).fetchone()
            if not schedule:raise ValueError('funding_schedule_missing')
            history=provider.funding_history(schedule[0]-1,mark['received_ms']) if mark['received_ms']>=schedule[0] else None
            advance(store,position_id=position['id'],mark_response=raw,received_ms=mark['received_ms'],history=history)
            filters=provider.filters('futures');trade=provider.futures_execution_trade(not_before_ms=intent['fixed_ms'])
            result=execute_exit(store,path/'queue.db',job_id=exit_job['id'],trade=trade,mark=mark,filters=filters['filters'],filters_ms=filters['received_ms'],clock=clock,fault=fault)
            return {'status':'CLOSED' if result['status'] in ('filled','filled_existing') else result['status'],'close':result['position']}
        if exit_job and exit_job['status']=='PENDING':
            reconcile_receipt(store,path/'queue.db',job_id=exit_job['id'])
            return {'status':'RECOVERED_CLOSE'}
        if entry and entry['status']=='DONE':raise ValueError('entry_done_position_missing_requires_review')
        if entry and entry['status']!='PENDING':raise ValueError('entry_reconciliation_required')
        if not entry:
            create_entry(queue,test_id=test_id,direction=direction,fixed_ms=clock());fault('after_entry_intent');entry=_job(queue,action)
        intent=json.loads(entry['payload'])
        filters=provider.filters('futures');mark=provider.futures_quote()
        trade=provider.futures_execution_trade(not_before_ms=intent['fixed_ms'])
        result=execute_entry(store,path/'queue.db',job_id=entry['id'],trade=trade,mark=mark,filters=filters['filters'],filters_ms=filters['received_ms'],next_funding_ms=mark['next_due_ms'],clock=clock,fault=fault)
        return {'status':'OPEN' if result['status'] in ('filled','filled_existing') else result['status'],'position':result['position']}
    finally:
        if store:store.close()
        queue.close()


def supervised(root, *, prefix, clock=lambda:int(time.time()*1000), sleeper=time.sleep,
               provider=public_feed, report=lambda value:None):
    """Hold the existing single-writer lock and run exactly two labelled tests.

    On any data failure, retain the position and retry with fresh evidence;
    no historical trade or funding is invented. The caller must arrange process
    recovery on unexpected termination before using this on persistent state.
    """
    from .adaptive_study_supervisor import writer_lock,cycle_lock
    root=Path(root);account=root/'baseline';sequence=((prefix+'-long','LONG'),(prefix+'-short','SHORT'))
    if not (account/'manifest.json').is_file() or json.loads((account/'manifest.json').read_text()).get('source')!='baseline':
        raise ValueError('research_account_identity_invalid')
    with writer_lock(root):
        for test_id,direction in sequence:
            while True:
                try:
                    with cycle_lock(root):
                        outcome=step(account,test_id=test_id,direction=direction,clock=clock,provider=provider)
                    report({'direction':direction,'status':outcome['status']})
                    if outcome['status']=='COMPLETE':break
                    sleeper(.2 if outcome['status'] in ('OPEN','CLOSED','RECOVERED_CLOSE') else 5)
                except Exception as exc:
                    # Never reset a position, fabricate an execution, or move on
                    # to SHORT when LONG remains unresolved.
                    reason=str(exc)[:120] if isinstance(exc,ValueError) else type(exc).__name__
                    report({'direction':direction,'status':'blocked','reason':reason})
                    sleeper(5)
    return {'status':'COMPLETE','pid':os.getpid()}


def publish_status(root, *, version, clock=lambda:int(time.time()*1000)):
    """Refresh a status from local ledgers; no market or AI request is made."""
    from .adaptive_study_status import snapshot
    from .adaptive_study_supervisor import cycle_lock,write_status
    root=Path(root)
    with cycle_lock(root):
        value=json.loads((root/'status.json').read_text())
        now=clock()
        value['checked_ms']=now
        value['code_version']=version
        value['experiments']={name:snapshot(root/name,quotes={},now_ms=now) for name in ('ai','baseline')}
        value['cycles']={name:{'status':'operator_only','reason':'strategy_collection_paused','protection':{'state':'unavailable' if value['experiments'][name]['positions'] else 'no_position'}} for name in ('ai','baseline')}
        value['data_errors']={'study':'strategy_collection_paused'}
        write_status(root,value)
    return {'checked_ms':now,'state':'operator_only'}


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--version',required=True)
    args=parser.parse_args()
    def report(value):
        print(json.dumps(value,sort_keys=True),flush=True)
        try:publish_status(args.root,version=args.version)
        except Exception:print('{"status":"local_status_unavailable"}',flush=True)
    supervised(args.root,prefix=args.prefix,report=report)
    report({'status':'COMPLETE'})


if __name__=='__main__':main()
