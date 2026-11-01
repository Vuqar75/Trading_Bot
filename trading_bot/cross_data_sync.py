"""Reconcile public cross data outside transactions before publishing quotes.

Offline integration prototype. Closed-position late corrections, process
supervision and Telegram wiring are still required before live deployment.
"""
import hashlib
import json
import time
from .cross_paper_book import SYMBOLS
from .cross_funding_tracker import CrossFundingTracker
from .paper_wallet_ledger import amount


class CrossDataSync:
    def __init__(self, book, reader):
        self.book=book;self.shared=book.shared;self.db=book.db;self.reader=reader
        self.tracker=CrossFundingTracker(book)

    def refresh(self, *, now=None):
        now=time.time() if now is None else now
        # Even if the process dies during I/O, previous quotes remain unusable.
        self.book.invalidate_all()
        try:
            schedules={symbol:self.reader.schedule(symbol) for symbol in SYMBOLS}
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
            events_by_symbol={}
            for symbol in SYMBOLS:
                relevant=[h for h in history.values() if h['position']['symbol']==symbol]
                if not relevant:continue
                schedule=schedules[symbol]
                events=self.reader.funding(symbol,min(h['position']['opened_ms'] for h in relevant),schedule['server_ms'])
                if not isinstance(events,list):raise ValueError('Funding history unavailable')
                events_by_symbol[symbol]=events
                returned={(e['time'],e.get('rate_type','Regular')) for e in events}
                if len(returned)!=len(events):raise ValueError('Duplicate funding history event')
                for item in relevant:
                    p=item['position'];end=item['closed_ms'] if item['closed_ms'] is not None else schedule['server_ms']
                    for known in self.db.execute('SELECT DISTINCT funding_time,rate_type FROM cross_funding WHERE position_id=?',(p['id'],)):
                        if p['opened_ms']<=known['funding_time']<=end and tuple(known) not in returned:
                            raise ValueError('Previously journaled funding disappeared from source')
                for event in events:
                    stamp=event['time'];kind=event.get('rate_type','Regular')
                    if event.get('symbol')!=symbol:raise ValueError('Funding symbol mismatch')
                    owners=[h for h in relevant if h['position']['opened_ms']<=stamp and (h['closed_ms'] is None or stamp<=h['closed_ms'])]
                    if len(owners)>1:raise ValueError('Ambiguous position funding ownership')
                    if not owners:continue
                    item=owners[0];p=item['position'];cursor=self.tracker._check_cursor(p['id'])
                    if item['closed_ms'] is None and stamp>cursor['next_due']:raise ValueError('Funding history outside known window')
                    previous=self.db.execute('SELECT revision,rate,mark FROM cross_funding WHERE position_id=? AND funding_time=? AND rate_type=? ORDER BY revision DESC LIMIT 1',(p['id'],stamp,kind)).fetchone()
                    if previous and amount(previous['rate'])==amount(event['rate']) and amount(previous['mark'])==amount(event['mark']):revision=previous['revision']
                    else:
                        revision=previous['revision']+1 if previous else 0
                        canonical=json.dumps([p['id'],stamp,kind,revision,event['rate'],event['mark']],separators=(',',':'))
                        operation='sync:'+hashlib.sha256(canonical.encode()).hexdigest()
                        fn=self.book.funding if item['closed_ms'] is None else self.book.late_funding
                        fn(operation,symbol,p['id'],stamp,revision,event['rate'],event['mark'],
                           'binance-usdm-public',now=now,rate_type=kind)
                    if stamp<cursor['next_due']:
                        receipt=self.db.execute('SELECT 1 FROM cross_correction_receipts WHERE position_id=? AND due=? AND rate_type=? AND revision=?',(p['id'],stamp,kind,revision)).fetchone()
                        if not receipt:self.tracker.confirm_correction(p['id'],{**event,'revision':revision})
            for symbol,p in ((p['symbol'],p) for p in active.values()):
                schedule=schedules[symbol];cursor=self.tracker._check_cursor(p['id'])
                due_events=[event for event in events_by_symbol.get(symbol,[]) if event['time']==cursor['next_due']]
                self.tracker.confirm(p['id'],expected_due=cursor['next_due'],server_ms=schedule['server_ms'],
                    observed_due=schedule['next_due_ms'],observed_interval=schedule['interval_ms'],events=due_events)
            quotes={symbol:{'mark':data['mark'],'time':now,'funding_complete':True,'server_ms':data['server_ms'],
                            'next_due_ms':data['next_due_ms'],'interval_ms':data['interval_ms']}
                    for symbol,data in schedules.items()}
            self.book.record_quotes(quotes,now=now)
            return quotes
        except Exception:
            self.book.fail_closed()
            raise
