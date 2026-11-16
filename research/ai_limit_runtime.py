"""Separate public-feed/paper worker and one-shot budget-owned AI analyst.

No Telegram poller or real exchange order transport. The paper loop never calls
an LLM; model downtime cannot stop its persisted protective exits.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import tempfile
import time

from .ai_limit_book import Book
from .ai_limit_feed import Feed
from .ai_limit_plan import VERSION,SYMBOLS,canonical,digest,validate,context,integer,dec,quote as validate_quote
from .ai_limit_executor import once,recover


def atomic(path,value,mode=0o644):
    path=Path(path)
    if not path.parent.is_dir() or path.is_symlink():raise ValueError('existing_public_directory_required')
    raw=canonical(value).encode()
    fd,name=tempfile.mkstemp(prefix='.ai-bracket-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,mode);os.replace(name,path)
        d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if os.path.exists(name):os.unlink(name)


def trusted(path,uid,limit=1000000):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_mode&0o022 or info.st_size>limit:raise ValueError('untrusted_public_input')
        raw=os.read(fd,limit+1)
        if len(raw)>limit:raise ValueError('public_input_oversize')
        return json.loads(raw)
    finally:os.close(fd)


def ingest(book,envelope,*,allow_fixture=False):
    if not isinstance(envelope,dict) or set(envelope)!={'analysis','capture'}:raise ValueError('model_envelope_shape')
    result=envelope['analysis'];capture=envelope['capture'];provenance=result['provenance']
    diagnostic=provenance.get('kind')=='executor_diagnostic'
    if not diagnostic and provenance.get('kind')!='budget_response' and not (allow_fixture and provenance.get('kind')=='fixture'):raise ValueError('live_worker_rejects_fixture')
    ident=('diagnostic:' if diagnostic else 'model:')+provenance['response_id'];fingerprint=digest(envelope)
    with book.transaction():
        prior=book.db.execute('SELECT fingerprint FROM bracket_inputs WHERE id=?',(ident,)).fetchone()
        if prior:
            if prior[0]!=fingerprint:raise ValueError('model_envelope_revision')
            return 'already_ingested'
    # Source registration/acceptance are independently idempotent. Do not hold
    # a second nested Book.transaction across a possible after_commit crash.
    admission=result.get('admission')
    if not isinstance(admission,dict):raise ValueError('model_admission_missing')
    status='REJECTED'
    if result.get('decision') is not None and not diagnostic:
        book.register_source(result['snapshot'],capture)
        try:
            if result['decision'].get('decision')!='WAIT' and book.get('market_error:'+result['snapshot']['symbol']):raise ValueError('entry_paused_blocked_or_busy')
            book.accept(result['snapshot'],result['decision'],provenance=provenance)
            status='WAIT' if result['decision']['decision']=='WAIT' else 'ACCEPTED'
        except ValueError as error:
            # A stale/busy/paused plan is terminal for this answer, not a reason
            # to replay or send another paid request.
            reason=str(error)
            allowed={'snapshot_expired','model_expiry','stale_or_future_market_data','entry_paused_blocked_or_busy',
                     'funding_coverage_missing','insufficient_paper_cash','limit_would_take_now','stale_filters'}
            if reason not in allowed:raise
            admission={'allowed':False,'reason':reason}
    with book.transaction():
        book.db.execute('INSERT OR IGNORE INTO bracket_inputs VALUES(?,?,?)',(ident,fingerprint,canonical(envelope)))
        if result.get('usage') is not None:book.set('last_api_summary',{'symbol':result['snapshot']['symbol'],'usage':result['usage'],'estimated_cost_micro_usd':result.get('estimated_cost_micro_usd'),'reserved_micro_usd':result.get('reserved_micro_usd'),'remaining_micro_usd':result.get('remaining_micro_usd')})
        if status=='REJECTED':book.set('analyst_last',{'status':'UNCONFIRMED' if diagnostic else 'REJECTED','symbol':result['snapshot']['symbol'],
            'reason':admission['reason'],'as_of_ms':book.clock(),'provenance':provenance})
    return status


class Poller:
    def __init__(self,book,feed,*,symbols=SYMBOLS,trade_executor=None,quote_executor=None):
        self.book=book;self.feed=feed;self.symbols=tuple(symbols);self.pool=ThreadPoolExecutor(max_workers=1)
        self.funding_job=None;self.next_funding_check=0;self.funding_cut=None;self.required_due=None
        self.filter_pool=ThreadPoolExecutor(max_workers=1);self.filter_jobs={};self.filter_failed=set()
        self.trade_pool=trade_executor or ThreadPoolExecutor(max_workers=1);self.trade_jobs={}
        self.quote_pool=quote_executor or ThreadPoolExecutor(max_workers=1);self.quote_jobs={}
        self.previous_timing={};self.previous_mono={};self.phase_history=deque(maxlen=8);self.loop_phase={}
        self.full_page_pending=False;self.full_page_streak={};self.drain_count=0;self.drain_deadline_ns=None
    def close(self):
        getattr(self.feed,"close",lambda:None)();self.pool.shutdown(wait=True,cancel_futures=True);self.filter_pool.shutdown(wait=True,cancel_futures=True);self.trade_pool.shutdown(wait=True,cancel_futures=True);self.quote_pool.shutdown(wait=True,cancel_futures=True)
    def funding_ready(self):
        b=self.book;now=b.clock()
        if self.funding_job is not None and self.funding_job.done():
            try:
                events=self.funding_job.result()
                for symbol,due in self.required_due.items():
                    if due and due<=self.funding_cut and not any(e['symbol']==symbol and abs(e['event_ms']-due)<=1 for e in events):raise ValueError('due_funding_not_published')
                b.funding(b.get('funding_through'),self.funding_cut,events)
                with b.transaction():b.set('funding_error',None)
            except (ValueError,OSError):
                with b.transaction():b.set('funding_error','funding_coverage_unconfirmed')
            self.funding_job=None
        if self.funding_job is None and now>=self.next_funding_check:
            self.funding_cut=min(now,b.get('funding_through')+7*86400000)
            start=b.get('funding_through')
            needed={p['symbol'] for p,s in b.plans() if s['opened_ms'] is not None and s['opened_ms']<=self.funding_cut and (s['status'] not in ('CLOSED','CANCELED') or s['last_fill_ms']>=start)}
            self.required_due={s:b.get('next_due:'+s) for s in needed}
            self.funding_job=self.pool.submit(self.feed.funding,start,self.funding_cut,symbols=sorted(needed)) if needed else self.pool.submit(lambda:[])
            self.next_funding_check=now+30000
    def sleep_after_cycle(self):
        if getattr(self.feed,"streaming",False):
            return .02 if self.feed.pending() else .1
        # At most two immediate catch-up cycles; each rechecks the quote and
        # protective exits. Never start an extra cycle after the 1 s deadline.
        now=time.monotonic_ns()
        if not self.full_page_pending:
            self.drain_count=0;self.drain_deadline_ns=None;return 2
        weight=getattr(self.feed,'weight_recent',None)
        if weight is None or not 0<=now-weight[1]<=5000000000 or weight[0]>=1000:
            self.drain_count=0;self.drain_deadline_ns=None;return 2
        if self.drain_deadline_ns is None:self.drain_deadline_ns=now+1000000000
        if self.drain_count<2 and now<=self.drain_deadline_ns:
            self.drain_count+=1;return 0
        self.drain_count=0;self.drain_deadline_ns=None;return 2

    def filters_ready(self,symbol):
        b=self.book;job=self.filter_jobs.get(symbol)
        if job is not None and job.done():
            try:
                b.refresh_filters(symbol,job.result());self.filter_failed.discard(symbol)
            except (ValueError,OSError,KeyError,TypeError):self.filter_failed.add(symbol)
            del self.filter_jobs[symbol];job=None
        current=b.get('filters:'+symbol);now=b.clock()
        age=now-current['as_of_ms'] if isinstance(current,dict) and type(current.get('as_of_ms')) is int else None
        if job is None and (age is None or age<0 or age>=50000):
            self.filter_jobs[symbol]=self.filter_pool.submit(self.feed.filters,symbol,force=True)
        ready=age is not None and 0<=age<60000 and symbol not in self.filter_failed
        if not ready:b.hold_unconfirmed_filters(symbol)
        return ready

    def cycle(self):
        self.book.protection_phase_context={'cycle_started_ns':time.monotonic_ns(),
                                            'prior_cycles':list(self.phase_history)[-2:]}
        if getattr(self.feed,'streaming',False):
            with self.book.grouped_transaction():return self._cycle()
        return self._cycle()

    def _cycle(self):
        b=self.book;self.full_page_pending=False;active=b.active();priority=[active[0]['symbol']] if active else []
        symbols=priority if priority else list(self.symbols)
        for symbol in symbols:
            tick=time.monotonic_ns();wall=b.clock();previous=self.previous_timing.get(symbol);phases={};stage='quote_request'
            try:
                initial=b.active();quote_job=self.quote_jobs.get(symbol)
                exposed=initial is not None and initial[0]['symbol']==symbol and dec(initial[1]['open_qty'])>0
                if quote_job is not None or exposed or getattr(self.feed,"streaming",False):
                    if quote_job is None:
                        quote_job=(self.quote_pool.submit(self.feed.book,symbol),time.monotonic_ns())
                        self.quote_jobs[symbol]=quote_job
                    future,quote_started=quote_job
                    if not future.done():
                        try:validate_quote(b.get('quote:'+symbol),symbol,b.clock());quote_ready=True
                        except (ValueError,KeyError,TypeError):quote_ready=False
                        if not quote_ready and b.active() is not None:
                            prior_quote=b.get('quote:'+symbol)
                            with b.transaction():b.set('last_quote_unconfirmed:'+symbol,{
                                'at_ms':b.clock(),'kind':'pending_http','pending_mono_ms':(time.monotonic_ns()-quote_started)//1000000,
                                'quote_source_ms':prior_quote.get('event_ms') if prior_quote else None,
                                'quote_age_ms':b.clock()-prior_quote['event_ms'] if prior_quote else None,
                                'quote_components':dict(getattr(self.feed,'quote_report',{}).get(symbol,{})),
                                'http_recent':list(getattr(self.feed,'http_timings',[]))[-8:]})
                            b.hold_unconfirmed_backlog(symbol,detail='quote_http_unconfirmed')
                            with b.transaction():b.set('market_error:'+symbol,'public_market_unconfirmed')
                        self.loop_phase['quote_http_pending_ms']=(time.monotonic_ns()-quote_started)//1000000
                        if not quote_ready:continue
                        q=b.get('quote:'+symbol);due=b.get('next_due:'+symbol)
                    else:
                        del self.quote_jobs[symbol]
                        try:q,due=future.result()
                        except (ValueError,KeyError,OSError,TypeError) as error:
                            prior_quote=b.get('quote:'+symbol)
                            failure={
                                'at_ms':b.clock(),'kind':'failed_http_or_validation','category':str(error) if str(error) in ('stale_or_future_market_data','quote_symbol','public_market_unavailable','public_market_http_failure') else type(error).__name__,
                                'pending_mono_ms':(time.monotonic_ns()-quote_started)//1000000,
                                'quote_source_ms':prior_quote.get('event_ms') if prior_quote else None,
                                'quote_age_ms':b.clock()-prior_quote['event_ms'] if prior_quote else None,
                                'quote_components':dict(getattr(self.feed,'quote_report',{}).get(symbol,{})),
                                'http_recent':list(getattr(self.feed,'http_timings',[]))[-8:]}
                            with b.transaction():
                                b.set('last_quote_unconfirmed:'+symbol,failure)
                                history=b.get('quote_failure_history:'+symbol,[])
                                b.set('quote_failure_history:'+symbol,(history+[failure])[-8:])
                            if b.active() is not None:b.hold_unconfirmed_backlog(symbol,detail='quote_http_unconfirmed')
                            raise
                else:q,due=self.feed.book(symbol)
                quote_done=time.monotonic_ns();stage='quote_apply'
                if getattr(self.feed,'streaming',False):
                    accepted=b.get('quote:'+symbol)
                    if accepted is not None and q['event_ms']<accepted['event_ms']:
                        # REST quote components can publish an older timestamp.
                        # Never overwrite a newer accepted quote or refresh its age.
                        validate_quote(accepted,symbol,b.clock())
                        with b.transaction():
                            b.set('last_quote_regression:'+symbol,{'received_ms':b.clock(),
                                  'discarded_ms':q['event_ms'],'retained_ms':accepted['event_ms']})
                        q=accepted;phases['older_quote_discarded']=True
                try:
                    validate_quote(q,symbol,b.clock())
                    if q!=b.get('quote:'+symbol):b.market('quote',q)
                except (ValueError,KeyError,OSError,TypeError):
                    if b.active() is not None:b.hold_unconfirmed_backlog(symbol,detail='quote_http_unconfirmed')
                    raise
                active_after_quote=b.active()
                if active_after_quote is not None and active_after_quote[0]['symbol']==symbol and dec(active_after_quote[1]['open_qty'])>0 and symbol not in self.quote_jobs:
                    self.quote_jobs[symbol]=(self.quote_pool.submit(self.feed.book,symbol),time.monotonic_ns())
                mark=time.monotonic_ns();phases['quote_finance_ms']=(mark-quote_done)//1000000
                stage='funding_due_sql'
                old=b.get('next_due:'+symbol)
                if old!=due and (old is None or old>b.clock() or old<=b.get('funding_through')):
                    with b.transaction():b.set('next_due:'+symbol,due)
                next_mark=time.monotonic_ns();phases['funding_due_sql_ms']=(next_mark-mark)//1000000;mark=next_mark
                stage='filters';filters_ready=self.filters_ready(symbol)
                next_mark=time.monotonic_ns();phases['filters_refresh_ms']=(next_mark-mark)//1000000;mark=next_mark
                phases['filters_ready']=filters_ready
                stage='trade_request';active_before=b.active();job=self.trade_jobs.get(symbol)
                # A slow public trade request must not hold quote protection
                # while a position exists. The network thread never writes DB.
                exposed=active_before is not None and active_before[0]['symbol']==symbol and dec(active_before[1]['open_qty'])>0
                if job is not None or (exposed and not getattr(self.feed,'streaming',False)):
                    if job is None:
                        requested=b.get('trade_cursor:'+symbol)
                        def fetch_trades(symbol=symbol,requested=requested):
                            values=self.feed.trades(symbol,requested)
                            return (*values,dict(getattr(self.feed,'trade_report',{}).get(symbol,{})))
                        job=(self.trade_pool.submit(fetch_trades),requested,time.monotonic_ns())
                        self.trade_jobs[symbol]=job
                    future,requested,started=job
                    if not future.done():
                        pending_ms=(time.monotonic_ns()-started)//1000000
                        confirmed=b.get('trade_confirmed_ms:'+symbol)
                        ready=(confirmed is not None and 0<=b.clock()-confirmed<=5000
                               and filters_ready and b.get('market_error:'+symbol) is None)
                        if not ready and b.active() is not None:b.hold_unconfirmed_backlog(symbol,detail='trade_http_unconfirmed')
                        with b.transaction():
                            if not ready:b.set('trade_confirmed_ms:'+symbol,None)
                            b.set('market_error:'+symbol,None if ready else 'public_trade_stream_unconfirmed')
                        self.loop_phase['trade_http_pending_ms']=pending_ms
                        continue
                    del self.trade_jobs[symbol]
                    try:events,cursor,gap,report=future.result()
                    except (ValueError,KeyError,OSError,TypeError):
                        if b.active() is not None:b.hold_unconfirmed_backlog(symbol,detail='trade_http_unconfirmed')
                        raise
                    if b.get('trade_cursor:'+symbol)!=requested:raise ValueError('trade_cursor_changed_while_pending')
                    # Downloaded rows must still be fresh when the DB writer
                    # receives them; old/future rows always remain fail-closed.
                    delayed=[e for e in events if not 0<=b.clock()-e['event_ms']<=5000]
                    if delayed:
                        gap=True;events=[e for e in events if 0<=b.clock()-e['event_ms']<=5000]
                        stamps=[e['event_ms'] for e in delayed if e['event_ms']<=b.clock()]
                        report['stale_rows']=report.get('stale_rows',0)+len(stamps)
                        if stamps:
                            report['stale_first_ms']=min([report['stale_first_ms']] + stamps) if report.get('stale_first_ms') is not None else min(stamps)
                            report['stale_last_ms']=max([report['stale_last_ms']] + stamps) if report.get('stale_last_ms') is not None else max(stamps)
                        report['future_rows']=report.get('future_rows',0)+len(delayed)-len(stamps)
                else:
                    events,cursor,gap=self.feed.trades(symbol,b.get('trade_cursor:'+symbol))
                    report=dict(getattr(self.feed,'trade_report',{}).get(symbol,{}))
                stage='trade_validation';next_mark=time.monotonic_ns();phases['trades_total_ms']=(next_mark-mark)//1000000
                full_page=report.get('rows')==1000
                self.full_page_streak[symbol]=self.full_page_streak.get(symbol,0)+1 if full_page else 0
                if full_page:
                    b.hold_unconfirmed_backlog(symbol)
                    # Persist the page before its financial processing. If an
                    # exception or restart occurs, the partial observation is
                    # retained without moving the durable execution cursor.
                    page={k:report.get(k) for k in ('requested_after_id','first_id','last_id','rows','first_event_ms','last_event_ms','started_ms','received_ms','fetch_ms','fetch_monotonic_ms','last_event_age_ms','newest_age_ms','oldest_age_ms','stale_rows','discontinuities','trade_validation_ms')}
                    page['consecutive_full_pages']=self.full_page_streak[symbol];page['processing']='pending'
                    with b.transaction():
                        history=b.get('full_page_history:'+symbol,[])
                        b.set('full_page_history:'+symbol,(history+[page])[-12:])
                report['poll_started_ms']=wall;report['quote_fetch_ms']=(quote_done-tick)//1000000
                if symbol in self.previous_mono:report['poll_interval_mono_ms']=(tick-self.previous_mono[symbol])//1000000
                self.previous_mono[symbol]=tick
                if previous is not None:
                    report['previous_cycle']=previous
                    report['poll_interval_ms']=max(0,wall-previous['poll_started_ms'])
                armed=active_before[1]['armed_ms'] if active_before else None
                report['active_armed_ms']=armed
                report['phase_diagnostics']={'clock':'monotonic_ns','cycle_started_ns':tick,'phases_ms':phases,
                    'http_recent':list(getattr(self.feed,'http_timings',deque()).copy())[-32:],
                    'prior_cycles':list(self.phase_history)[-4:]}
                if gap:
                    # An exposed plan may have no entry remainder to cancel.
                    # Preserve the failed quote before the next pending refresh
                    # overwrites the latest-status key; never infer a cause.
                    report['phase_diagnostics']['quote_unconfirmed']=b.get('last_quote_unconfirmed:'+symbol)
                    report['phase_diagnostics']['quote_failures']=b.get('quote_failure_history:'+symbol,[])
                # A stale, contiguous prefix ending before the current plan was
                # armed cannot have filled or protected that plan. ID gaps,
                # future rows and stale events after arming remain fail-closed.
                prearm=bool(gap and armed is not None and report.get('stale_rows',0)>0
                    and report.get('stale_last_ms') is not None and report['stale_last_ms']<=armed
                    and type(report.get('requested_after_id')) is int
                    and not report.get('discontinuities') and not report.get('future_rows',0))
                if prearm:gap=False;report['ignored_prearm_stale_rows']=report['stale_rows']
                report['history_contiguous']=type(report.get('requested_after_id')) is int and not bool(report.get('discontinuities'))
                report['execution_gap']=bool(gap)
                stage='gap_handling'
                if gap:
                    gap_evidence={k:v for k,v in report.items() if k!='phase_diagnostics'}
                    b.trade_gap(symbol,'noncontiguous_or_stale_public_trades_no_backfills',evidence=gap_evidence,
                                phase_evidence=report['phase_diagnostics'])
                    # Reset a lagging cursor ONLY while financially idle. The
                    # bootstrap batch is never replayed into execution. A later
                    # contiguous, fresh poll must confirm the stream first.
                    if b.active() is None:
                        _,reset_cursor,_=self.feed.trades(symbol)
                        if reset_cursor is None or (cursor is not None and reset_cursor<cursor):raise ValueError('trade_resync_cursor_regression')
                        cursor=reset_cursor
                        report={**report,'idle_resync':getattr(self.feed,'trade_report',{}).get(symbol,{})}
                        events=[]
                stage='batch_commit';waiting=time.monotonic_ns()
                with b.transaction():
                    locked=time.monotonic_ns();active=b.active();skipped=0
                    if prearm and (active is None or active_before[0]['plan_id']!=active[0]['plan_id']
                                   or active[1]['armed_ms']!=armed):raise ValueError('active_plan_changed_during_batch')
                    if active is None:
                        # Ingestion and arming run in the same single writer;
                        # the durable cursor and this no-risk check share the
                        # transaction, so idle trades cannot become backfills.
                        skipped=len(events)
                    elif (getattr(self.feed,'streaming',False) and not gap
                          and all(0<=b.clock()-e['event_ms']<=5000 for e in events)
                          and all((dec(e['price'])>=dec(active[0]['entry']) and dec(e['price'])<=dec(active[0]['target']))
                                  if active[0]['direction']=='LONG' else
                                  (dec(e['price'])<=dec(active[0]['entry']) and dec(e['price'])>=dec(active[0]['target']))
                                  for e in events)):
                        # Strict trade-through cannot execute anywhere in this
                        # interval. Quote protection already ran; pulse follows.
                        # Preserve source hash/cursor, not idle tick rows.
                        report['non_actionable_events_skipped']=len(events)
                    else:
                        for event in events:
                            if not 0<=b.clock()-event['event_ms']<=5000:
                                b.hold_unconfirmed_backlog(symbol,detail='processing_lag');gap=True;break
                            b.market('trade',event)
                    processed=time.monotonic_ns()
                    last_event=events[-1]['event_ms'] if events else b.get('trade_confirmed_ms:'+symbol)
                    healthy=not gap and last_event is not None and 0<=b.clock()-last_event<=5000
                    report['batch_processing_ms']=(processed-locked)//1000000
                    report['batch_lock_wait_ms']=(locked-waiting)//1000000
                    report['idle_events_skipped']=skipped
                    if full_page:
                        history=b.get('full_page_history:'+symbol,[])
                        if not history or history[-1].get('started_ms')!=page['started_ms']:raise ValueError('page_history_changed')
                        page.update({'gap':bool(gap),'cursor_after':cursor,'processing':'committed',
                                     'batch_processing_ms':report['batch_processing_ms'],'batch_lock_wait_ms':report['batch_lock_wait_ms']})
                        history[-1]=page;b.set('full_page_history:'+symbol,history)
                        report['full_page_history']=history
                    b.set('trade_cursor:'+symbol,cursor);b.set('trade_feed:'+symbol,report)
                    b.set('trade_confirmed_ms:'+symbol,last_event if healthy else None)
                    b.set('market_error:'+symbol,('public_filters_unconfirmed' if not filters_ready else ('public_trade_backlog_unconfirmed' if full_page else (None if healthy else 'public_trade_stream_unconfirmed'))))
                self.full_page_pending=full_page and not gap and b.active() is None
                self.loop_phase['poller_symbol_ms']=(time.monotonic_ns()-tick)//1000000
                self.previous_timing[symbol]={'poll_started_ms':wall,'total_ms':(time.monotonic_ns()-tick)//1000000,
                    'batch_finalize_ms':(time.monotonic_ns()-processed)//1000000,'batch_processing_ms':report['batch_processing_ms'],
                    'batch_lock_wait_ms':report['batch_lock_wait_ms'],'event_count':len(events),'idle_events_skipped':skipped}
            except (ValueError,KeyError,OSError,TypeError) as error:
                # Preserve only an allowlisted category, never arbitrary
                # exception text, response bodies or credentials. Updating a
                # bounded metadata ring shares the existing failure transaction.
                allowed={'public_market_unavailable','public_market_http_failure',
                         'public_access_denied_http401','public_access_denied_http403',
                         'public_access_denied_http451','public_response_size','public_response_json',
                         'stale_or_future_market_data','quote_symbol','trade_list','trade_symbol_or_side',
                         'market_out_of_order','market_event_revision','trade_cursor_changed_while_pending',
                         'trade_resync_cursor_regression','active_plan_changed_during_batch','page_history_changed'}
                reason=str(error);reason=reason if reason in allowed else 'other'
                event={'at_ms':b.clock(),'stage':stage,'class':type(error).__name__,'reason':reason,
                       'elapsed_ms':(time.monotonic_ns()-tick)//1000000}
                with b.transaction():
                    b.set('market_error:'+symbol,'public_market_unconfirmed')
                    b.set('trade_confirmed_ms:'+symbol,None)
                    history=b.get('poll_error_history:'+symbol,[])
                    b.set('poll_error_history:'+symbol,(history+[event])[-8:])
        phase_start=time.monotonic_ns();b.pulse();phase_end=time.monotonic_ns()
        self.loop_phase['pulse_finance_ms']=(phase_end-phase_start)//1000000;phase_start=phase_end
        self.funding_ready();phase_end=time.monotonic_ns();self.loop_phase['funding_check_ms']=(phase_end-phase_start)//1000000
        phase_start=time.monotonic_ns();result=b.snapshot();self.loop_phase['poller_snapshot_ms']=(time.monotonic_ns()-phase_start)//1000000
        result['market_errors']={s:b.get('market_error:'+s) for s in SYMBOLS};result['funding_error']=b.get('funding_error')
        result['last_trade_gap']=b.get('last_trade_gap')
        result['trade_feeds']={s:b.get('trade_feed:'+s) for s in symbols}
        return result


def analyst(budget_state,paper_public,output,*,symbol,paper_uid,feed=None,clock=lambda:int(time.time()*1000),transport=None,secret=None):
    now=clock();status=trusted(paper_public,paper_uid)
    if status.get('version')!=VERSION or status.get('paper_only') is not True or status.get('ledger_balanced') is not True or not 0<=now-status['as_of_ms']<=15000:raise ValueError('paper_status_untrusted_or_stale')
    if status['paused'] or status['blocked'] or any(p['status'] not in ('CLOSED','CANCELED') for p in status['plans']):return {'status':'not_requested_paper_paused_or_busy'}
    if status.get('funding_error') or now-status['funding_through_ms']>120000 or status.get('market_errors',{}).get(symbol):
        reasons=[]
        if status.get('funding_error'):reasons.append('funding_error')
        if now-status['funding_through_ms']>120000:reasons.append('funding_coverage_stale')
        error=status.get('market_errors',{}).get(symbol)
        if error:reasons.append(error if error in ('public_market_unconfirmed','public_trade_stream_unconfirmed') else 'market_unconfirmed')
        return {'status':'not_requested_market_or_funding_unconfirmed','blocking_reasons':reasons,
                'paper_as_of_ms':status['as_of_ms'],'funding_through_ms':status['funding_through_ms']}
    provider=feed or Feed(clock=clock);snapshot,capture=provider.snapshot(symbol)
    kwargs={'authorized':True,'clock':clock}
    if transport is not None:kwargs['transport']=transport
    if secret is not None:kwargs['secret']=secret
    try:result=once(budget_state,snapshot,**kwargs)
    except Exception as error:
        reason=str(error)
        if not re.fullmatch('[a-z_0-9]{1,100}',reason):reason='model_attempt_unconfirmed'
        diagnostic={'snapshot':snapshot,'decision':None,'admission':{'allowed':False,'reason':reason},
            'provenance':{'kind':'executor_diagnostic','response_id':snapshot['snapshot_id']+':'+str(clock()),'model':'gpt-6-luna'}}
        atomic(output,{'analysis':diagnostic,'capture':capture})
        raise
    atomic(output,{'analysis':result,'capture':capture})
    return {'status':'published','admission':result['admission'],'decision':result['decision']['decision'] if result['decision'] else None,'network_retry':False}


class ResponseReader:
    """Reuse only an already-ingested atomic file identity; never repeat HTTP."""
    def __init__(self):self.accepted=None
    def apply(self,book,path,owner):
        info=os.stat(path,follow_symlinks=False)
        identity=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_uid,info.st_mode)
        if identity==self.accepted:return 'unchanged'
        result=ingest(book,trusted(path,owner))
        # Do not cache failed ingestion or a file replaced during the read.
        after=os.stat(path,follow_symlinks=False)
        if identity==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_uid,after.st_mode):self.accepted=identity
        return result

class Publisher:
    def __init__(self):self.next_ns=0
    def publish(self,path,result,*,force=False):
        now=time.monotonic_ns()
        if force or now>=self.next_ns:
            atomic(path,result);self.next_ns=now+250000000;return True
        return False


def uid(value):
    return int(value) if str(value).isdigit() else pwd.getpwnam(value).pw_uid


def activate_initial(book,enabled):
    if not enabled:return
    with book.transaction():
        if not book.plans() and book.get('activation_recorded') is None and book.get('operator_pause_recorded') is not True:
            book.set('paused',False);book.set('activation_recorded',book.clock())


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('serve');p.add_argument('--state',required=True);p.add_argument('--public',required=True);p.add_argument('--response',required=True)
    p.add_argument('--analyst-uid',required=True,type=uid);p.add_argument('--symbols',nargs='+',choices=SYMBOLS,default=['BTCUSDT']);p.add_argument('--enable-paper',action='store_true');p.add_argument('--once',action='store_true')
    p=sub.add_parser('analyst-once');p.add_argument('--budget-state',required=True);p.add_argument('--paper-public',required=True);p.add_argument('--output',required=True)
    p.add_argument('--paper-uid',required=True,type=uid);p.add_argument('--symbol',choices=SYMBOLS,default='BTCUSDT');p.add_argument('--authorized-once',action='store_true')
    p=sub.add_parser('recover-answer');p.add_argument('--budget-state',required=True);p.add_argument('--response-id',required=True);p.add_argument('--capture',required=True);p.add_argument('--output',required=True)
    p=sub.add_parser('status');p.add_argument('--state',required=True)
    p=sub.add_parser('pause');p.add_argument('--state',required=True)
    p=sub.add_parser('resume');p.add_argument('--state',required=True)
    p=sub.add_parser('close');p.add_argument('--state',required=True);p.add_argument('--plan-id',required=True)
    p=sub.add_parser('backup');p.add_argument('--state',required=True);p.add_argument('--target',required=True)
    args=parser.parse_args();os.umask(0o077)
    if args.action=='analyst-once':
        if not args.authorized_once:raise ValueError('explicit_paper_plan_attempt_required')
        result=analyst(args.budget_state,args.paper_public,args.output,symbol=args.symbol,paper_uid=args.paper_uid)
    elif args.action=='recover-answer':
        result=recover(args.budget_state,args.response_id);capture=json.loads(Path(args.capture).read_text())
        atomic(args.output,{'analysis':result,'capture':capture});result={'status':'recovered_without_http'}
    else:
        # Status does not create/reseed a missing database.
        if args.action!='serve' and not (Path(args.state)/'paper.sqlite').is_file():raise ValueError('existing_paper_state_required')
        book=Book(args.state)
        try:
            if args.action=='serve':
                # On restart this flag MUST NOT override an operator pause.
                activate_initial(book,args.enable_paper)
                running=[True]
                def stop(*_):running[0]=False
                signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
                import sys
                sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"vendor"))
                from .ai_limit_stream import StreamFeed
                worker=Poller(book,StreamFeed(symbols=args.symbols).start(),symbols=args.symbols);next_backup=0;reader=ResponseReader();publisher=Publisher()
                try:
                    while running[0]:
                        loop_start=time.monotonic_ns();phase={'started_ns':loop_start};worker.loop_phase=phase
                        mark=time.monotonic_ns();result=worker.cycle();now=time.monotonic_ns();phase['poller_total_ms']=(now-mark)//1000000;mark=now
                        if Path(args.response).exists():
                            try:reader.apply(book,args.response,args.analyst_uid)
                            except (ValueError,OSError,KeyError,TypeError):
                                with book.transaction():book.set('analyst_ingest_error','response_unavailable_or_invalid')
                        now=time.monotonic_ns();phase['ingest_ms']=(now-mark)//1000000;mark=now
                        result=book.snapshot()|{k:v for k,v in result.items() if k in ('market_errors','funding_error','last_trade_gap','trade_feeds')}
                        now=time.monotonic_ns();phase['snapshot_ms']=(now-mark)//1000000;mark=now
                        publisher.publish(args.public,result,force=args.once)
                        now=time.monotonic_ns();phase['publish_ms']=(now-mark)//1000000;mark=now
                        if book.clock()>=next_backup:
                            folder=book.root/'backups';folder.mkdir(mode=0o700,exist_ok=True)
                            book.backup(folder/(str(book.clock())+'.sqlite'))
                            for old in sorted(folder.glob('[0-9]*.sqlite'))[:-24]:
                                if not old.is_symlink() and old.stat().st_uid==os.geteuid():old.unlink()
                            next_backup=book.clock()+3600000
                        now=time.monotonic_ns();phase['backup_ms']=(now-mark)//1000000;mark=now
                        if args.once:
                            worker.phase_history.append(phase);break
                        pause=worker.sleep_after_cycle();phase['sleep_requested_ms']=int(pause*1000);time.sleep(pause)
                        phase['sleep_ms']=(time.monotonic_ns()-mark)//1000000;phase['total_ms']=(time.monotonic_ns()-loop_start)//1000000
                        worker.phase_history.append(phase)
                finally:worker.close()
            elif args.action=='pause':book.pause(True);result=book.snapshot()
            elif args.action=='resume':book.pause(False);result=book.snapshot()
            elif args.action=='close':book.request_close(args.plan_id);result=book.snapshot()
            elif args.action=='backup':result={'backup':str(book.backup(args.target))}
            else:result=book.snapshot()
        finally:book.close()
    print(canonical(result))


if __name__=='__main__':
    try:main()
    except Exception as error:
        # Never print provider/credential content or arbitrary exception bodies.
        import re
        reason=str(error)
        print(canonical({'status':'blocked','reason':reason if re.fullmatch('[a-z_0-9]{1,100}',reason) else type(error).__name__,'network_retry':False}))
        raise SystemExit(1)
