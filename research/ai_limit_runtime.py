"""Separate public-feed/paper worker and one-shot budget-owned AI analyst.

No Telegram poller or real exchange order transport. The paper loop never calls
an LLM; model downtime cannot stop its persisted protective exits.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
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
from .ai_limit_plan import VERSION,SYMBOLS,canonical,digest,validate,context,integer
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
    def __init__(self,book,feed,*,symbols=SYMBOLS):
        self.book=book;self.feed=feed;self.symbols=tuple(symbols);self.pool=ThreadPoolExecutor(max_workers=1)
        self.funding_job=None;self.next_funding_check=0;self.funding_cut=None;self.required_due=None
    def close(self):self.pool.shutdown(wait=True,cancel_futures=True)
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
    def cycle(self):
        b=self.book;active=b.active();priority=[active[0]['symbol']] if active else []
        symbols=priority if priority else list(self.symbols)
        for symbol in symbols:
            try:
                q,due=self.feed.book(symbol);b.market('quote',q)
                with b.transaction():
                    old=b.get('next_due:'+symbol)
                    if old is None or old>b.clock() or old<=b.get('funding_through'):b.set('next_due:'+symbol,due)
                old=b.get('filters:'+symbol)
                if old is None or b.clock()-old['as_of_ms']>=60000:b.refresh_filters(symbol,self.feed.filters(symbol))
                events,cursor,gap=self.feed.trades(symbol,b.get('trade_cursor:'+symbol))
                if gap:b.trade_gap(symbol,'noncontiguous_or_stale_public_trades_no_backfills')
                for event in events:b.market('trade',event)
                with b.transaction():b.set('trade_cursor:'+symbol,cursor);b.set('market_error:'+symbol,None)
            except (ValueError,KeyError,OSError,TypeError):
                with b.transaction():b.set('market_error:'+symbol,'public_market_unconfirmed')
        b.pulse();self.funding_ready()
        result=b.snapshot();result['market_errors']={s:b.get('market_error:'+s) for s in SYMBOLS};result['funding_error']=b.get('funding_error')
        result['last_trade_gap']=b.get('last_trade_gap')
        return result


def analyst(budget_state,paper_public,output,*,symbol,paper_uid,feed=None,clock=lambda:int(time.time()*1000),transport=None,secret=None):
    now=clock();status=trusted(paper_public,paper_uid)
    if status.get('version')!=VERSION or status.get('paper_only') is not True or status.get('ledger_balanced') is not True or not 0<=now-status['as_of_ms']<=15000:raise ValueError('paper_status_untrusted_or_stale')
    if status['paused'] or status['blocked'] or any(p['status'] not in ('CLOSED','CANCELED') for p in status['plans']):return {'status':'not_requested_paper_paused_or_busy'}
    if status.get('funding_error') or now-status['funding_through_ms']>120000 or status.get('market_errors',{}).get(symbol):return {'status':'not_requested_market_or_funding_unconfirmed'}
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


def uid(value):
    return int(value) if str(value).isdigit() else pwd.getpwnam(value).pw_uid


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('serve');p.add_argument('--state',required=True);p.add_argument('--public',required=True);p.add_argument('--response',required=True)
    p.add_argument('--analyst-uid',required=True,type=uid);p.add_argument('--symbols',nargs='+',choices=SYMBOLS,default=['BTCUSDT']);p.add_argument('--enable-paper',action='store_true');p.add_argument('--once',action='store_true')
    p=sub.add_parser('analyst-once');p.add_argument('--budget-state',required=True);p.add_argument('--paper-public',required=True);p.add_argument('--output',required=True)
    p.add_argument('--paper-uid',required=True,type=uid);p.add_argument('--symbol',choices=SYMBOLS,default='BTCUSDT');p.add_argument('--authorized-once',action='store_true')
    p=sub.add_parser('recover-answer');p.add_argument('--budget-state',required=True);p.add_argument('--response-id',required=True);p.add_argument('--capture',required=True);p.add_argument('--output',required=True)
    p=sub.add_parser('status');p.add_argument('--state',required=True)
    p=sub.add_parser('pause');p.add_argument('--state',required=True)
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
                if args.enable_paper and not book.plans() and book.get('activation_recorded') is None:
                    book.pause(False)
                    with book.transaction():book.set('activation_recorded',book.clock())
                running=[True]
                def stop(*_):running[0]=False
                signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
                worker=Poller(book,Feed(),symbols=args.symbols);next_backup=0
                try:
                    while running[0]:
                        result=worker.cycle()
                        if Path(args.response).exists():
                            try:ingest(book,trusted(args.response,args.analyst_uid))
                            except (ValueError,OSError,KeyError,TypeError):
                                with book.transaction():book.set('analyst_ingest_error','response_unavailable_or_invalid')
                        result=book.snapshot()|{k:v for k,v in result.items() if k in ('market_errors','funding_error','last_trade_gap')}
                        atomic(args.public,result)
                        if book.clock()>=next_backup:
                            folder=book.root/'backups';folder.mkdir(mode=0o700,exist_ok=True)
                            book.backup(folder/(str(book.clock())+'.sqlite'))
                            for old in sorted(folder.glob('[0-9]*.sqlite'))[:-24]:
                                if not old.is_symlink() and old.stat().st_uid==os.geteuid():old.unlink()
                            next_backup=book.clock()+3600000
                        if args.once:break
                        time.sleep(2)
                finally:worker.close()
            elif args.action=='pause':book.pause(True);result=book.snapshot()
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
