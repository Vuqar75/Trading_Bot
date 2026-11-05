"""Reconcile public cross data outside transactions before publishing quotes.

Offline integration prototype. Closed-position late corrections, process
supervision and Telegram wiring are still required before live deployment.
"""
import hashlib
import json
import re
import time
from .cross_paper_book import SYMBOLS
from .cross_funding_tracker import CrossFundingTracker
from .paper_wallet_ledger import amount
from .cross_funding_period import assign_period
from .testnet_observer import ObservationError


class FundingReconciliationRequired(ValueError):
    """A previously accepted published event has changed."""


class FundingHistoryContradiction(FundingReconciliationRequired):
    """A previously accepted interval no longer matches public history."""


class CrossDataSync:
    def __init__(self, book, reader, *, diagnostic=None):
        self.book=book;self.shared=book.shared;self.db=book.db;self.reader=reader
        self.tracker=CrossFundingTracker(book)
        self.page_status='not_checked';self.model_status='not_checked'
        self.diagnostic=diagnostic

    def _diagnose(self,phase,stage,started,exc=None):
        if self.diagnostic is None:return
        item={'phase':phase,'stage':stage,'started_at':started,'observed_at':time.time()}
        if exc is not None:
            name=type(exc).__name__
            item['exception_type']=name if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}',name) else 'Exception'
        try:self.diagnostic(item)
        except Exception:pass  # Observability must never interrupt protection.

    def _check_retired_history(self, symbol, events):
        """Detect contradictions before any new funding postings for a retired pair."""
        windows=self.db.execute(
            "SELECT DISTINCT r.due,r.next_due FROM cross_coverage_receipts r "
            "JOIN cross_funding_cursor c ON c.position_id=r.position_id WHERE c.symbol=? "
            "UNION SELECT DISTINCT r.due,r.next_due FROM cross_prior_interval_receipts r "
            "JOIN cross_funding_cursor c ON c.position_id=r.position_id WHERE c.symbol=?",(symbol,symbol)).fetchall()
        known=self.db.execute('SELECT funding_time,rate_type,rate,mark,source FROM cross_funding WHERE symbol=?',(symbol,)).fetchall()
        def matches(row,event):
            prefix='binance-usdm-public:fundingTime='
            original=int(row['source'][len(prefix):]) if row['source'].startswith(prefix) else row['funding_time']
            return (event.get('time')==original and event.get('symbol')==symbol and
                    event.get('rate_type','Regular')==row['rate_type'] and
                    amount(event['rate'])==amount(row['rate']) and amount(event['mark'])==amount(row['mark']))
        for row in known:
            if sum(bool(matches(row,event)) for event in events)!=1:
                raise FundingHistoryContradiction('Accepted retired funding missing or changed')
        for window in windows:
            candidates=[e for e in events if window['due']<=e.get('time',-1)<window['next_due']]
            if len(candidates)!=1:
                raise FundingHistoryContradiction('Accepted retired funding interval ambiguous')

    def refresh(self, *, now=None):
        now=time.time() if now is None else now
        started=time.time();stage='invalidate';self._diagnose('refreshing','fetch_or_reconcile',started)
        self.page_status='not_checked';self.model_status='not_checked'
        try:
            # Even if the process dies during I/O, previous quotes remain unusable.
            self.book.invalidate_all()
            selected=self.book._symbols()
            conflict=self.db.execute("SELECT value FROM cross_runtime WHERE key='funding_history_conflict'").fetchone()
            retired_row=self.db.execute("SELECT value FROM cross_runtime WHERE key='retired_funding_conflicts'").fetchone()
            retired=set(json.loads(retired_row[0])) if retired_row else set()
            if conflict and (not getattr(self.book,'_pair_experiment',False) or not retired or retired.intersection(selected)):
                raise FundingHistoryContradiction('Funding history contradiction requires explicit reconciliation')
            stage='schedule';schedules={symbol:self.reader.schedule(symbol) for symbol in selected}
            active={p['id']:p for p in self.book._positions()}
            history={}
            rows=self.db.execute("SELECT kind,created,details FROM ledger_events WHERE kind IN ('CROSS_OPEN','CROSS_CLOSE') ORDER BY created,id").fetchall()
            for row in rows:
                if row['kind']!='CROSS_OPEN':continue
                data=json.loads(row['details'])
                if data['id'] in history:raise ValueError('Duplicate position opening')
                history[data['id']]={'position':data,'closed_ms':None}
            for row in rows:
                if row['kind']!='CROSS_CLOSE':continue
                data=json.loads(row['details']);ident=data['position_id']
                if ident not in history or history[ident]['closed_ms'] is not None:raise ValueError('Position history incomplete')
                closed=int(row['created']*1000)
                if closed<history[ident]['position']['opened_ms']:raise ValueError('Position close precedes opening')
                history[ident]['closed_ms']=closed
            for ident,item in history.items():
                if (ident in active)!=(item['closed_ms'] is None):raise ValueError('Position lifecycle mismatch')
            if retired and (not conflict or any(not any(h['position']['symbol']==s for h in history.values()) or
                any(h['position']['symbol']==s and h['closed_ms'] is None for h in history.values()) for s in retired)):
                raise ValueError('Retired funding conflict state invalid')
            # Removed symbols still need funding history rechecked. Their last
            # closed position supplies the bounded lifecycle; only selected
            # symbols supply live risk quotes.
            history_symbols=sorted(set(selected)|{h['position']['symbol'] for h in history.values()})
            end_ms=max(data['server_ms'] for data in schedules.values())
            for symbol in history_symbols:
                if symbol not in schedules:schedules[symbol]={'server_ms':end_ms}
            stage='history';loaded={}
            historical_unavailable=set()
            for symbol in history_symbols:
                if symbol in retired:continue
                relevant=[h for h in history.values() if h['position']['symbol']==symbol]
                if relevant:
                    try:
                        events=self.reader.funding(symbol,min(h['position']['opened_ms'] for h in relevant),schedules[symbol]['server_ms'])
                        if not isinstance(events,list):raise ValueError('Funding history unavailable')
                        for event in events:
                            if (not isinstance(event,dict) or event.get('symbol')!=symbol or type(event.get('time')) is not int or
                                event.get('rate_type','Regular') not in ('Regular','Special')):
                                raise ObservationError('Invalid funding history')
                            try:amount(event['rate']);amount(event['mark'],positive=True)
                            except (KeyError,TypeError,ValueError):raise ObservationError('Invalid funding history') from None
                    except (ObservationError,OSError,TimeoutError,ValueError):
                        if symbol in selected or any(h['closed_ms'] is None for h in relevant):raise
                        historical_unavailable.add(symbol)
                        continue
                    loaded[symbol]=events
            # Preflight every retired source before applying any new funding.
            # Preserve accepted history and pause permanently on contradiction.
            for symbol in history_symbols:
                if symbol in selected or symbol in retired or symbol in historical_unavailable or symbol not in loaded:continue
                try:self._check_retired_history(symbol,loaded[symbol])
                except FundingHistoryContradiction:
                    retired.add(symbol)
                    with self.shared.transaction():
                        self.db.execute("INSERT OR REPLACE INTO cross_runtime(key,value) VALUES('retired_funding_conflicts',?)",(json.dumps(sorted(retired)),))
                        self.db.execute("INSERT OR IGNORE INTO cross_runtime(key,value) VALUES('funding_history_conflict','true')")
                        self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
            self.page_status=('historical_contradiction' if retired else
                              'historical_unconfirmed' if historical_unavailable else 'loaded_available_pages')
            stage='reconcile';events_by_symbol={}
            for symbol in history_symbols:
                if symbol in historical_unavailable or symbol in retired:continue
                relevant=[h for h in history.values() if h['position']['symbol']==symbol]
                if not relevant:continue
                schedule=schedules[symbol]
                events=loaded[symbol]
                # Recheck every previously accepted half-open period before
                # any new posting. A later second event or changed publication
                # is a contradiction, never an automatic financial correction.
                windows=self.db.execute(
                    "SELECT DISTINCT r.due,r.next_due FROM cross_coverage_receipts r "
                    "JOIN cross_funding_cursor c ON c.position_id=r.position_id WHERE c.symbol=? "
                    "UNION SELECT DISTINCT r.due,r.next_due FROM cross_prior_interval_receipts r "
                    "JOIN cross_funding_cursor c ON c.position_id=r.position_id WHERE c.symbol=?",(symbol,symbol)).fetchall()
                for window in windows:
                    candidates=[event for event in events if window['due']<=event.get('time',-1)<window['next_due']]
                    stored=self.db.execute(
                        "SELECT f.funding_time,f.rate_type,f.rate,f.mark,f.source FROM cross_funding f "
                        "WHERE f.symbol=? AND f.funding_time=? AND (EXISTS "
                        "(SELECT 1 FROM cross_coverage_receipts r WHERE r.position_id=f.position_id AND r.due=f.funding_time AND r.next_due=?) OR EXISTS "
                        "(SELECT 1 FROM cross_prior_interval_receipts r WHERE r.position_id=f.position_id AND r.due=f.funding_time AND r.next_due=?)) ORDER BY f.revision DESC",
                        (symbol,window['due'],window['next_due'],window['next_due'])).fetchall()
                    if len(candidates)!=1 or len(stored)!=1:
                        raise FundingHistoryContradiction('Previously accepted funding interval has missing or ambiguous events')
                    event=candidates[0];item=stored[0];prefix='binance-usdm-public:fundingTime='
                    original=int(item['source'][len(prefix):]) if item['source'].startswith(prefix) else item['funding_time']
                    if (event.get('time')!=original or event.get('rate_type','Regular')!=item['rate_type'] or
                        amount(event['rate'])!=amount(item['rate']) or amount(event['mark'])!=amount(item['mark'])):
                        raise FundingHistoryContradiction('Accepted funding event changed; manual reconciliation required')
                for item in relevant:
                    if item['closed_ms'] is not None:continue
                    cursor=self.tracker._check_cursor(item['position']['id'])
                    due,interval=cursor['next_due'],cursor['interval_ms']
                    if schedule['server_ms']>=due:
                        candidates=[event for event in events if type(event.get('time')) is int and due<=event['time']<due+interval]
                        if not candidates:raise FundingReconciliationRequired('Expected funding payment is not published')
                        if len(candidates)!=1:raise FundingReconciliationRequired('Duplicate or ambiguous funding period')
                        if candidates[0].get('rate_type','Regular')!='Regular':
                            raise FundingReconciliationRequired('Special funding period is not proven')
                # Paper policy: associate a published time with the single
                # evidenced half-open funding interval, while retaining the
                # exchange's original millisecond timestamp in the source.
                # Only the current active interval or an already journaled
                # association can be mapped; all other cases remain fail closed.
                mapped=[]
                for original in events:
                    event=dict(original);raw=event['time'];kind=event.get('rate_type','Regular')
                    source='binance-usdm-public:fundingTime='+str(raw)
                    prior=self.db.execute("SELECT DISTINCT funding_time,source FROM cross_funding WHERE symbol=? AND rate_type=? AND (source=? OR (source='binance-usdm-public' AND funding_time=?))",(symbol,kind,source,raw)).fetchall()
                    if len(prior)>1:raise ValueError('Ambiguous reported funding timestamp')
                    if prior:
                        event['time']=prior[0][0]
                        if prior[0][1]!='binance-usdm-public':event['reported_time']=raw
                    else:
                        matches=[]
                        for item in relevant:
                            if item['closed_ms'] is not None and raw>=item['closed_ms']:continue
                            p=item['position'];cursor=self.tracker._check_cursor(p['id'])
                            due=cursor['next_due'];interval=cursor['interval_ms']
                            previous=self.db.execute('SELECT due,next_due FROM cross_schedule_windows WHERE symbol=? AND next_due=? AND due<=? AND ?<next_due',(symbol,due,raw,raw)).fetchall()
                            if previous and p['opened_ms']>=previous[0]['due'] and p['opened_ms']<=raw:
                                if self.book.observed_funding_window(symbol,previous[0]['due'])!=due:raise ValueError('Observed funding schedule evidence mismatch')
                                candidates=[x for x in events if previous[0]['due']<=x['time']<previous[0]['next_due']]
                                if len(previous)!=1 or len(candidates)!=1 or kind!='Regular':raise FundingReconciliationRequired('Ambiguous prior funding period')
                                matches.append(assign_period(raw,[(previous[0]['due'],due)],opened_ms=p['opened_ms']))
                            elif item['closed_ms'] is None and due<raw<due+interval:
                                if kind!='Regular':raise ValueError('Special funding period is not proven')
                                if (schedule['interval_ms']!=interval or schedule['next_due_ms']!=due+interval or
                                    not due<=schedule['server_ms']<due+interval or raw>schedule['server_ms']):
                                    raise ValueError('Funding period schedule not proven')
                                matches.append(assign_period(raw,[(due,due+interval)],opened_ms=p['opened_ms']))
                        if len(matches)>1:raise ValueError('Ambiguous funding period ownership')
                        if matches:event['time']=matches[0];event['reported_time']=raw
                    mapped.append(event)
                events=mapped
                events_by_symbol[symbol]=events
                returned={(e['time'],e.get('rate_type','Regular')) for e in events}
                if len(returned)!=len(events):raise FundingReconciliationRequired('Duplicate funding history event')
                for item in relevant:
                    p=item['position'];end=item['closed_ms'] if item['closed_ms'] is not None else schedule['server_ms']
                    for known in self.db.execute('SELECT DISTINCT funding_time,rate_type FROM cross_funding WHERE position_id=?',(p['id'],)):
                        if p['opened_ms']<=known['funding_time']<=end and tuple(known) not in returned:
                            raise FundingReconciliationRequired('Previously journaled funding disappeared from source')
                for event in events:
                    stamp=event['time'];kind=event.get('rate_type','Regular')
                    if event.get('symbol')!=symbol:raise ValueError('Funding symbol mismatch')
                    actual=event.get('reported_time',stamp)
                    owners=[h for h in relevant if h['position']['opened_ms']<=actual and (h['closed_ms'] is None or actual<h['closed_ms'])]
                    if len(owners)>1:raise ValueError('Ambiguous position funding ownership')
                    if not owners:continue
                    item=owners[0];p=item['position'];cursor=self.tracker._check_cursor(p['id'])
                    if item['closed_ms'] is None and stamp>cursor['next_due']:raise ValueError('Funding history outside known window')
                    previous=self.db.execute('SELECT revision,rate,mark,source FROM cross_funding WHERE position_id=? AND funding_time=? AND rate_type=? ORDER BY revision DESC LIMIT 1',(p['id'],stamp,kind)).fetchone()
                    event_source=('binance-usdm-public:fundingTime='+str(event['reported_time']) if 'reported_time' in event else 'binance-usdm-public')
                    if previous and ('reported_time' in event or previous['source'].startswith('binance-usdm-public:fundingTime=')) and previous['source']!=event_source:
                        raise FundingReconciliationRequired('Reported funding timestamp changed')
                    if previous and (amount(previous['rate'])!=amount(event['rate']) or amount(previous['mark'])!=amount(event['mark'])):
                        raise FundingReconciliationRequired('Accepted funding event changed; manual reconciliation required')
                    if previous:revision=previous['revision']
                    else:
                        revision=0
                        canonical=json.dumps([p['id'],stamp,kind,revision,event['rate'],event['mark']],separators=(',',':'))
                        operation='sync:'+hashlib.sha256(canonical.encode()).hexdigest()
                        fn=self.book.funding if item['closed_ms'] is None else self.book.late_funding
                        fn(operation,symbol,p['id'],stamp,revision,event['rate'],event['mark'],
                           event_source,now=now,rate_type=kind)
                    if stamp<cursor['next_due']:
                        receipt=self.db.execute('SELECT 1 FROM cross_correction_receipts WHERE position_id=? AND due=? AND rate_type=? AND revision=?',(p['id'],stamp,kind,revision)).fetchone()
                        prior_receipt=self.db.execute('SELECT 1 FROM cross_prior_interval_receipts WHERE position_id=? AND due=?',(p['id'],stamp)).fetchone()
                        if not receipt and not prior_receipt:self.tracker.confirm_correction(p['id'],{**event,'revision':revision})
            for symbol,p in ((p['symbol'],p) for p in active.values()):
                schedule=schedules[symbol];cursor=self.tracker._check_cursor(p['id'])
                due_events=[event for event in events_by_symbol.get(symbol,[]) if event['time']==cursor['next_due']]
                self.tracker.confirm(p['id'],expected_due=cursor['next_due'],server_ms=schedule['server_ms'],
                    observed_due=schedule['next_due_ms'],observed_interval=schedule['interval_ms'],events=due_events)
            quotes={symbol:{'mark':data['mark'],'time':now,'funding_complete':True,'server_ms':data['server_ms'],
                            'next_due_ms':data['next_due_ms'],'interval_ms':data['interval_ms']}
                    for symbol,data in schedules.items() if symbol in selected}
            stage='publish';self.book.record_quotes(quotes,now=now)
            if retired:
                self.book.set_runtime_error('FundingHistoryContradiction')
                self.model_status='historical_contradiction'
            elif historical_unavailable:
                self.book.set_runtime_error('HistoricalFundingUnavailable')
                with self.shared.transaction():
                    self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
                self.model_status='historical_unconfirmed'
            else:
                self.model_status='matches_paper_assumption'
                if self.book.runtime_error()=='HistoricalFundingUnavailable':
                    self.book.set_runtime_error(None)
            self._diagnose('complete' if self.model_status=='matches_paper_assumption' else 'unconfirmed','complete',started)
            return quotes
        except Exception as exc:
            self._diagnose('failed',stage,started,exc)
            if self.page_status!='loaded_available_pages':self.page_status='not_confirmed'
            if isinstance(exc,FundingHistoryContradiction):self.model_status='contradiction'
            elif self.page_status=='loaded_available_pages':self.model_status='unconfirmed'
            if isinstance(exc,FundingHistoryContradiction):
                with self.shared.transaction():
                    self.db.execute("INSERT OR IGNORE INTO cross_runtime(key,value) VALUES('funding_history_conflict','true')")
                    self.db.execute('UPDATE cross_quotes SET funding_complete=0')
                    self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
            else:self.book.fail_closed()
            raise
