"""Experimental durable funding cursor for offline cross rehearsals.

Not integrated with market entry or quote publication; it is not a deployment
gate. Fetch data outside SQLite transactions and invalidate quotes before using
this component. It never fills an absent funding payment with zero.
"""
import hashlib
import json

from .cross_funding_coverage import check_funding_window
from .paper_wallet_ledger import amount, encode


class CrossFundingTracker:
    def __init__(self, book):
        self.book=book;self.shared=book.shared;self.db=book.db
        with self.shared.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_funding_cursor(position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, next_due INTEGER NOT NULL, interval_ms INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_cursor_anchor(position_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES ledger_events(id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_coverage_receipts(position_id TEXT NOT NULL, due INTEGER NOT NULL, next_due INTEGER NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(position_id,due))')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_correction_receipts(position_id TEXT NOT NULL, due INTEGER NOT NULL, rate_type TEXT NOT NULL, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(position_id,due,rate_type,revision))')
            for verb in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_cross_coverage_{verb.lower()} BEFORE {verb} ON cross_coverage_receipts BEGIN SELECT RAISE(ABORT,'immutable cross coverage'); END")
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_cross_correction_{verb.lower()} BEFORE {verb} ON cross_correction_receipts BEGIN SELECT RAISE(ABORT,'immutable cross correction'); END")

    def _check_cursor(self, position_id):
        row=self.db.execute('SELECT * FROM cross_funding_cursor WHERE position_id=?',(position_id,)).fetchone()
        anchor=self.db.execute('SELECT e.details FROM cross_cursor_anchor a JOIN ledger_events e ON e.id=a.event_id WHERE a.position_id=?',(position_id,)).fetchone()
        if not row or not anchor or json.loads(anchor[0]).get('cursor') != dict(row):
            raise ValueError('Funding cursor journal mismatch')
        return row

    def _anchor_cursor(self, position_id, event_id):
        row=self.db.execute('SELECT * FROM cross_funding_cursor WHERE position_id=?',(position_id,)).fetchone()
        self.book.ledger._post(event_id,'FINANCIAL_METADATA',{'cursor':dict(row)},[])
        self.db.execute('INSERT OR REPLACE INTO cross_cursor_anchor VALUES(?,?)',(position_id,event_id))

    @staticmethod
    def _correction_fingerprint(event):
        canonical={'symbol':event['symbol'],'time':event['time'],'rate_type':event.get('rate_type','Regular'),
                   'revision':event['revision'],'rate':encode(amount(event['rate'])),'mark':encode(amount(event['mark'],positive=True))}
        return hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def _save_correction(self, position_id, due, kind, revision, event):
        fingerprint=self._correction_fingerprint(event)
        self.book.ledger._post('cross:correction-coverage:'+position_id+':'+str(due)+':'+kind+':'+str(revision),
            'FINANCIAL_METADATA',{'position_id':position_id,'due':due,'rate_type':kind,'revision':revision,'fingerprint':fingerprint},[])
        self.db.execute('INSERT OR IGNORE INTO cross_correction_receipts VALUES(?,?,?,?,?)',(position_id,due,kind,revision,fingerprint))

    def start(self, position_id, symbol, due, interval_ms, server_ms):
        if any(type(x) is not int or x<=0 for x in (due,interval_ms,server_ms)) or due<=server_ms or due-server_ms>interval_ms:
            raise ValueError('Invalid initial funding schedule')
        with self.shared.transaction():
            self.book._check()
            p=self.db.execute('SELECT symbol FROM cross_positions WHERE id=?',(position_id,)).fetchone()
            if not p or p[0]!=symbol:raise ValueError('Unknown funding position')
            old=self.db.execute('SELECT symbol,next_due,interval_ms FROM cross_funding_cursor WHERE position_id=?',(position_id,)).fetchone()
            if old:
                self._check_cursor(position_id)
                if tuple(old)!=(symbol,due,interval_ms):raise ValueError('Conflicting initial funding schedule')
                return
            self.db.execute('INSERT INTO cross_funding_cursor VALUES(?,?,?,?)',(position_id,symbol,due,interval_ms))
            self._anchor_cursor(position_id,'cross:cursor:init:'+position_id)

    def confirm(self, position_id, *, expected_due, server_ms, observed_due, observed_interval, events):
        try:
            return self._confirm(position_id,expected_due=expected_due,server_ms=server_ms,observed_due=observed_due,
                                 observed_interval=observed_interval,events=events)
        except (ValueError,TypeError,KeyError):
            self.book.fail_closed()
            raise

    def _confirm(self, position_id, *, expected_due, server_ms, observed_due, observed_interval, events):
        with self.shared.transaction():
            self.book._check()
            if not isinstance(events,list) or any(not isinstance(event,dict) for event in events):
                raise ValueError('Invalid funding events')
            canonical=json.dumps([observed_due,observed_interval,
                sorted(json.dumps(event,sort_keys=True,separators=(',',':')) for event in events)],separators=(',',':'))
            fingerprint=hashlib.sha256(canonical.encode()).hexdigest()
            row=self._check_cursor(position_id)
            receipt=self.db.execute('SELECT next_due,fingerprint FROM cross_coverage_receipts WHERE position_id=? AND due=?',(position_id,expected_due)).fetchone()
            if receipt:
                if receipt['fingerprint']!=fingerprint:raise ValueError('Conflicting funding coverage replay')
                return {'complete':True,'next_due':receipt['next_due'],'events':events}
            if self.db.execute('SELECT 1 FROM cross_quotes WHERE funding_complete!=0 LIMIT 1').fetchone():
                raise ValueError('Invalidate all quotes before funding reconciliation')
            position=self.db.execute('SELECT symbol FROM cross_positions WHERE id=?',(position_id,)).fetchone()
            if not row or not position or row['symbol']!=position['symbol']:
                raise ValueError('Funding cursor or position missing')
            if row['next_due']!=expected_due:raise ValueError('Funding cursor superseded')
            result=check_funding_window(prior_due=row['next_due'],prior_interval=row['interval_ms'],server_ms=server_ms,
                                        observed_due=observed_due,observed_interval=observed_interval,events=events)
            expected={}
            for event in result['events']:
                kind=event.get('rate_type','Regular')
                if event.get('symbol')!=row['symbol']:
                    raise ValueError('Funding event symbol conflict')
                try:expected[kind]=(amount(event['rate']),amount(event['mark'],positive=True))
                except (KeyError,ValueError):raise ValueError('Invalid funding event values') from None
            stored={}
            for funding in self.db.execute('SELECT rate_type,revision,rate,mark FROM cross_funding WHERE position_id=? AND funding_time=? ORDER BY revision',(position_id,row['next_due'])):
                stored[funding['rate_type']]=(amount(funding['rate']),amount(funding['mark']))
            if stored!=expected:raise ValueError('Funding events not fully journaled')
            previous=row['next_due']
            if result['next_due']!=previous:
                self.db.execute('UPDATE cross_funding_cursor SET next_due=? WHERE position_id=?',(result['next_due'],position_id))
                self._anchor_cursor(position_id,'cross:coverage:'+position_id+':'+str(previous))
                self.db.execute('INSERT INTO cross_coverage_receipts VALUES(?,?,?,?)',(position_id,previous,result['next_due'],fingerprint))
                for item in self.db.execute('SELECT symbol,funding_time,rate_type,revision,rate,mark FROM cross_funding WHERE position_id=? AND funding_time=? AND revision>0',(position_id,previous)).fetchall():
                    event={'symbol':item['symbol'],'time':item['funding_time'],'rate_type':item['rate_type'],'revision':item['revision'],'rate':item['rate'],'mark':item['mark']}
                    self._save_correction(position_id,previous,item['rate_type'],item['revision'],event)
            return result

    def confirm_correction(self, position_id, event):
        try:
            return self._confirm_correction(position_id,event)
        except (ValueError,TypeError,KeyError):
            self.book.fail_closed();raise

    def _confirm_correction(self, position_id, event):
        if not isinstance(event,dict):raise ValueError('Invalid funding correction')
        with self.shared.transaction():
            self.book._check();cursor=self._check_cursor(position_id)
            due=event.get('time');kind=event.get('rate_type','Regular');revision=event.get('revision')
            if type(due) is not int or kind not in ('Regular','Special') or type(revision) is not int or revision<=0:
                raise ValueError('Invalid funding correction identity')
            fingerprint=self._correction_fingerprint(event)
            old=self.db.execute('SELECT fingerprint FROM cross_correction_receipts WHERE position_id=? AND due=? AND rate_type=? AND revision=?',(position_id,due,kind,revision)).fetchone()
            if old:
                if old[0]!=fingerprint:raise ValueError('Conflicting funding correction replay')
                return
            if self.db.execute('SELECT 1 FROM cross_quotes WHERE funding_complete!=0 LIMIT 1').fetchone():
                raise ValueError('Invalidate all quotes before funding correction')
            if due>=cursor['next_due'] or event.get('symbol')!=cursor['symbol'] or not self.db.execute('SELECT 1 FROM cross_coverage_receipts WHERE position_id=? AND due=?',(position_id,due)).fetchone():
                raise ValueError('Correction does not reference confirmed funding')
            current=self.db.execute('SELECT revision,rate,mark FROM cross_funding WHERE position_id=? AND funding_time=? AND rate_type=? ORDER BY revision DESC LIMIT 1',(position_id,due,kind)).fetchone()
            if not current or current['revision']!=revision or amount(current['rate'])!=amount(event.get('rate')) or amount(current['mark'])!=amount(event.get('mark'),positive=True):
                raise ValueError('Funding correction not journaled')
            self._save_correction(position_id,due,kind,revision,event)
