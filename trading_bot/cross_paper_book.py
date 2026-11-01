"""Offline cross-margin accounting on the existing paper journal.

This module has no network or Telegram transport.  It is not a runtime entry
point and must not be connected to the live VM until integration is complete.
"""
import hashlib
import json
import time
import uuid
from decimal import Decimal, localcontext

from .paper_wallet_ledger import amount, encode

SYMBOLS = ('BTCUSDT', 'ETHUSDT')
WALLET = 'futures_cross'
FEE_BPS = Decimal('5')
SLIP_BPS = Decimal('2')
MAINTENANCE_BPS = Decimal('50')  # Explicit paper assumption, not Binance tiers.


class CrossPaperBook:
    def __init__(self, shared):
        self.shared, self.db, self.ledger = shared, shared.db, shared.ledger
        with shared.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_positions(symbol TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, direction TEXT NOT NULL, quantity TEXT NOT NULL, entry TEXT NOT NULL, margin TEXT NOT NULL, leverage INTEGER NOT NULL, funding TEXT NOT NULL, opened_ms INTEGER NOT NULL)')
            if 'opened_ms' not in {row[1] for row in self.db.execute('PRAGMA table_info(cross_positions)')}:
                raise ValueError('Unsupported experimental cross position schema')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_operations(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_anchor(id INTEGER PRIMARY KEY CHECK(id=1), event_id TEXT NOT NULL REFERENCES ledger_events(id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_settings(id INTEGER PRIMARY KEY CHECK(id=1), leverage INTEGER NOT NULL, interval TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_funding(position_id TEXT NOT NULL, symbol TEXT NOT NULL, funding_time INTEGER NOT NULL, rate_type TEXT NOT NULL, revision INTEGER NOT NULL, rate TEXT NOT NULL, mark TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(position_id,funding_time,rate_type,revision))')
            if 'rate_type' not in {row[1] for row in self.db.execute('PRAGMA table_info(cross_funding)')}:
                raise ValueError('Unsupported experimental cross funding schema')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_quotes(symbol TEXT PRIMARY KEY, mark TEXT NOT NULL, observed REAL NOT NULL, funding_complete INTEGER NOT NULL CHECK(funding_complete IN (0,1)), server_ms INTEGER NOT NULL, next_due_ms INTEGER NOT NULL, interval_ms INTEGER NOT NULL)')
            if 'next_due_ms' not in {row[1] for row in self.db.execute('PRAGMA table_info(cross_quotes)')}:
                raise ValueError('Unsupported experimental cross quote schema')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_funding_cursor(position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, next_due INTEGER NOT NULL, interval_ms INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_cursor_anchor(position_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES ledger_events(id))')
            for verb in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_cross_funding_{verb.lower()} BEFORE {verb} ON cross_funding BEGIN SELECT RAISE(ABORT,'immutable cross funding'); END")
            self.db.execute("INSERT OR IGNORE INTO wallet_state(wallet,paused) VALUES('futures_cross',1)")
            if not self.db.execute('SELECT 1 FROM cross_settings WHERE id=1').fetchone():
                if self._positions():raise ValueError('Missing cross settings')
                self.db.execute("INSERT INTO cross_settings VALUES(1,1,'15m')")
            if not self.db.execute('SELECT 1 FROM cross_anchor WHERE id=1').fetchone():
                if self._positions() or self.db.execute("SELECT 1 FROM ledger_events WHERE id LIKE 'cross:anchor:%' LIMIT 1").fetchone():
                    raise ValueError('Missing cross anchor')
                self._anchor('initial')
            self._check()

    def _positions(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM cross_positions ORDER BY symbol')]

    def _anchor(self, operation):
        event='cross:anchor:'+operation
        self.ledger._post(event,'FINANCIAL_METADATA',{'after':self._positions()},[])
        self.db.execute('INSERT OR REPLACE INTO cross_anchor VALUES(1,?)',(event,))

    def _check(self):
        if not self.ledger.reconcile()['balanced']:
            raise ValueError('Journal integrity mismatch')
        positions = self._positions()
        row=self.db.execute('SELECT e.details FROM cross_anchor a JOIN ledger_events e ON e.id=a.event_id WHERE a.id=1').fetchone()
        if not row or json.loads(row[0]).get('after') != positions:
            raise ValueError('Cross position journal mismatch')
        from .cross_funding_tracker import CrossFundingTracker
        tracker=object.__new__(CrossFundingTracker);tracker.book=self;tracker.shared=getattr(self,'shared',None);tracker.db=self.db
        for position in positions:
            cursor=tracker._check_cursor(position['id'])
            if cursor['symbol']!=position['symbol']:raise ValueError('Funding cursor symbol mismatch')
        with localcontext() as ctx:
            ctx.prec = 120
            if sum((amount(p['margin']) for p in positions), Decimal(0)) != self.ledger.balance(WALLET+':margin'):
                raise ValueError('Cross margin mismatch')
            if sum((amount(p['funding']) for p in positions), Decimal(0)) != self.ledger.balance(WALLET+':funding'):
                raise ValueError('Cross funding mismatch')
        if self.ledger.balance(WALLET+':free') < 0 or self.ledger.balance(WALLET+':reserved') < 0:
            raise ValueError('Negative cross free or reserve')

    def set_paused(self, paused, *, now=None):
        if type(paused) is not bool:
            raise ValueError('Invalid cross pause')
        with self.shared.transaction():
            self._check()
            if not paused:
                if self.shared.deployment_hold():raise ValueError('Deployment hold is active')
                current=time.time() if now is None else now
                self._require_coverage(current)
                marks=self._marks(self.stored_quotes(),current)
                if self._risk(marks)['buffer'] <= 0:raise ValueError('Cross margin buffer exhausted')
            self.db.execute("UPDATE wallet_state SET paused=? WHERE wallet='futures_cross'", (int(paused),))

    def set_settings(self, *, leverage, interval):
        from .futures_data import INTERVALS
        if type(leverage) is not int or not 1 <= leverage <= 10 or interval not in INTERVALS:
            raise ValueError('Invalid cross settings')
        with self.shared.transaction():
            self._check()
            if not self.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0] or self._positions():
                raise ValueError('Pause cross and close all positions before changing settings')
            self.db.execute('UPDATE cross_settings SET leverage=?,interval=? WHERE id=1',(leverage,interval))

    def record_quotes(self, quotes, *, now=None):
        """Persist a complete offline data snapshot; caller must prove funding coverage."""
        try:
            return self._record_quotes(quotes,now=now)
        except (ValueError,TypeError,KeyError):
            self.fail_closed()
            raise

    def _record_quotes(self, quotes, *, now=None):
        now = time.time() if now is None else now
        marks = self._marks(quotes, now)
        with self.shared.transaction():
            self._check()
            pending=self.db.execute('SELECT position_id,funding_time,rate_type,revision FROM cross_funding WHERE revision>0').fetchall()
            if pending:
                if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_correction_receipts'").fetchone():
                    raise ValueError('Cross funding correction requires reconciliation')
                for item in pending:
                    if not self.db.execute('SELECT 1 FROM cross_correction_receipts WHERE position_id=? AND due=? AND rate_type=? AND revision=?',tuple(item)).fetchone():
                        raise ValueError('Cross funding correction requires reconciliation')
            for symbol in SYMBOLS:
                seen = float(quotes[symbol]['time'])
                server,due,interval=(quotes[symbol][k] for k in ('server_ms','next_due_ms','interval_ms'))
                if any(type(x) is not int or x<=0 for x in (server,due,interval)) or not server<due<=server+interval or abs(seen*1000-server)>60000:
                    raise ValueError('Invalid cross funding schedule')
                position=self.db.execute('SELECT id FROM cross_positions WHERE symbol=?',(symbol,)).fetchone()
                if position:
                    from .cross_funding_tracker import CrossFundingTracker
                    tracker=object.__new__(CrossFundingTracker);tracker.book=self;tracker.shared=self.shared;tracker.db=self.db
                    cursor=tracker._check_cursor(position['id'])
                    if int(now*1000)>=cursor['next_due'] or server>=cursor['next_due'] or due!=cursor['next_due'] or interval!=cursor['interval_ms']:
                        raise ValueError('Cross funding coverage requires reconciliation')
                previous = self.db.execute('SELECT mark,observed,server_ms FROM cross_quotes WHERE symbol=?',(symbol,)).fetchone()
                if previous and (seen < previous['observed'] or server<previous['server_ms'] or (seen == previous['observed'] and amount(previous['mark']) != marks[symbol])):
                    raise ValueError('Contradictory cross quote')
                self.db.execute('INSERT OR REPLACE INTO cross_quotes VALUES(?,?,?,1,?,?,?)',(symbol,encode(marks[symbol]),seen,server,due,interval))

    def stored_quotes(self):
        return {r['symbol']:{'mark':r['mark'],'time':r['observed'],'funding_complete':bool(r['funding_complete']),
                              'server_ms':r['server_ms'],'next_due_ms':r['next_due_ms'],'interval_ms':r['interval_ms']}
                for r in self.db.execute('SELECT * FROM cross_quotes')}

    def invalidate_data(self, symbol):
        if symbol not in SYMBOLS:
            raise ValueError('Invalid cross symbol')
        with self.shared.transaction():
            self.db.execute('UPDATE cross_quotes SET funding_complete=0 WHERE symbol=?',(symbol,))

    def invalidate_all(self):
        with self.shared.transaction():
            self.db.execute('UPDATE cross_quotes SET funding_complete=0')

    def fail_closed(self):
        with self.shared.transaction():
            self.db.execute('UPDATE cross_quotes SET funding_complete=0')
            self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")

    def _current_marks(self, quotes, now):
        self._require_coverage(now)
        supplied=self._marks(quotes,now)
        stored=self.stored_quotes()
        actual=self._marks(stored,now)
        for symbol in SYMBOLS:
            if supplied[symbol]!=actual[symbol] or float(quotes[symbol]['time'])!=float(stored[symbol]['time']):
                raise ValueError('Cross quote snapshot superseded')
        return actual

    def _require_coverage(self, now):
        from .cross_funding_tracker import CrossFundingTracker
        tracker=object.__new__(CrossFundingTracker);tracker.book=self;tracker.shared=getattr(self,'shared',None);tracker.db=self.db
        for position in self._positions():
            cursor=tracker._check_cursor(position['id'])
            if int(now*1000)>=cursor['next_due']:
                raise ValueError('Cross funding coverage requires reconciliation')

    @staticmethod
    def _marks(quotes, now):
        marks = {}
        for symbol in SYMBOLS:
            q = quotes.get(symbol) if isinstance(quotes, dict) else None
            if not isinstance(q, dict) or q.get('funding_complete') is not True:
                raise ValueError('Incomplete cross data: '+symbol)
            try:
                age = now-float(q['time'])
                mark = amount(q['mark'], positive=True)
            except (TypeError, KeyError, ValueError, OverflowError):
                raise ValueError('Invalid cross mark: '+symbol) from None
            if not 0 <= age <= 120:
                raise ValueError('Stale cross mark: '+symbol)
            marks[symbol] = mark
        return marks

    def _risk(self, marks):
        positions = self._positions()
        free = self.ledger.balance(WALLET+':free')
        reserved = self.ledger.balance(WALLET+':reserved')
        margin = self.ledger.balance(WALLET+':margin')
        funding = self.ledger.balance(WALLET+':funding')
        gross = maintenance = exit_cost = Decimal(0)
        for p in positions:
            q, mark, entry = amount(p['quantity']), marks[p['symbol']], amount(p['entry'])
            direction = Decimal(1) if p['direction'] == 'LONG' else Decimal(-1)
            gross += (mark-entry)*q*direction
            maintenance += q*mark*MAINTENANCE_BPS/10000
            exit_fill = mark*(1-direction*SLIP_BPS/10000)
            exit_cost += q*(mark-exit_fill)*direction + q*exit_fill*FEE_BPS/10000
        equity = free+reserved+margin+funding+gross
        available = max(Decimal(0), min(free, equity-margin-reserved-exit_cost))
        return {'free':free, 'reserved':reserved, 'margin':margin, 'funding':funding,
                'gross':gross, 'equity':equity, 'maintenance':maintenance,
                'exit_cost':exit_cost, 'buffer':equity-maintenance-exit_cost,
                'available':available, 'positions':positions}

    def risk(self, quotes, *, now=None):
        now = time.time() if now is None else now
        with self.shared.transaction(), localcontext() as ctx:
            ctx.prec = 120
            self._check()
            return self._risk(self._current_marks(quotes, now))

    def _prior(self, operation, data):
        if not isinstance(operation, str) or not operation:
            raise ValueError('Operation identity required')
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row = self.db.execute('SELECT fingerprint,result FROM cross_operations WHERE id=?', (operation,)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint:
                raise ValueError('Conflicting operation identity')
            return fingerprint, json.loads(row['result'])
        return fingerprint, None

    def _remember(self, operation, fingerprint, result):
        self.db.execute('INSERT INTO cross_operations VALUES(?,?,?)', (operation, fingerprint, json.dumps(result, sort_keys=True)))

    def open(self, operation, symbol, direction, margin, leverage, quotes, *, now=None, expected_interval=None):
        now = time.time() if now is None else now
        if symbol not in SYMBOLS or direction not in ('LONG','SHORT') or type(leverage) is not int or not 1 <= leverage <= 10:
            raise ValueError('Invalid cross position')
        margin = amount(margin, positive=True)
        if margin > 5:
            raise ValueError('Cross margin exceeds paper limit')
        data = ['open',symbol,direction,encode(margin),leverage,expected_interval]
        with self.shared.transaction(), localcontext() as ctx:
            ctx.prec = 120
            self._check()
            fingerprint, previous = self._prior(operation, data)
            if previous is not None:
                return previous
            if self.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]:
                raise ValueError('Cross is paused')
            if self.db.execute('SELECT leverage FROM cross_settings WHERE id=1').fetchone()[0] != leverage:
                raise ValueError('Leverage does not match cross settings')
            if expected_interval is not None and self.db.execute('SELECT interval FROM cross_settings WHERE id=1').fetchone()[0] != expected_interval:
                raise ValueError('Interval does not match cross settings')
            marks = self._current_marks(quotes, now)
            risk = self._risk(marks)
            if risk['buffer'] <= 0 or risk['available'] < margin + margin*leverage*FEE_BPS/10000:
                raise ValueError('Insufficient cross collateral')
            if len(risk['positions']) >= 2 or any(p['symbol'] == symbol for p in risk['positions']):
                raise ValueError('Cross symbol already occupied')
            mark = marks[symbol]
            fill = mark*(1+(1 if direction == 'LONG' else -1)*SLIP_BPS/10000)
            with localcontext() as quantity_context:
                quantity_context.prec = 40
                quantity = margin*leverage/fill
            fee = quantity*fill*FEE_BPS/10000
            pos = {'symbol':symbol,'id':uuid.uuid4().hex,'direction':direction,'quantity':encode(quantity),
                   'entry':encode(fill),'margin':encode(margin),'leverage':leverage,'funding':'0','opened_ms':int(now*1000)}
            self.ledger._post('cross:open:'+operation,'CROSS_OPEN',pos,
                [(WALLET+':free','USDT',-margin),(WALLET+':margin','USDT',margin),
                 (WALLET+':free','USDT',-fee),('external:cross_fees','USDT',fee)],created=now)
            self.db.execute('INSERT INTO cross_positions VALUES(:symbol,:id,:direction,:quantity,:entry,:margin,:leverage,:funding,:opened_ms)',pos)
            from .cross_funding_tracker import CrossFundingTracker
            tracker=object.__new__(CrossFundingTracker);tracker.book=self;tracker.shared=self.shared;tracker.db=self.db
            schedule=self.db.execute('SELECT next_due_ms,interval_ms,server_ms FROM cross_quotes WHERE symbol=?',(symbol,)).fetchone()
            if not schedule or schedule['server_ms']>=schedule['next_due_ms']:raise ValueError('Cross funding schedule unavailable')
            self.db.execute('INSERT INTO cross_funding_cursor VALUES(?,?,?,?)',(pos['id'],symbol,schedule['next_due_ms'],schedule['interval_ms']))
            tracker._anchor_cursor(pos['id'],'cross:cursor:init:'+pos['id'])
            if self._risk(marks)['buffer'] <= 0:
                raise ValueError('Cross maintenance buffer exhausted')
            self._anchor(operation);self._check();self._remember(operation,fingerprint,pos)
            return pos

    def funding(self, operation, symbol, position_id, stamp, revision, rate, mark, source, *, now=None, rate_type='Regular'):
        try:
            return self._funding(operation,symbol,position_id,stamp,revision,rate,mark,source,now=now,rate_type=rate_type)
        except (ValueError,TypeError,KeyError):
            self.fail_closed()
            raise

    def _funding(self, operation, symbol, position_id, stamp, revision, rate, mark, source, *, now=None, rate_type='Regular'):
        now = time.time() if now is None else now
        if symbol not in SYMBOLS or rate_type not in ('Regular','Special') or type(stamp) is not int or stamp < 0 or type(revision) is not int or revision < 0 or not source:
            raise ValueError('Invalid funding identity')
        rate, mark = amount(rate), amount(mark, positive=True)
        if abs(rate) >= 1:
            raise ValueError('Invalid funding rate')
        data = ['funding',symbol,position_id,stamp,rate_type,revision,encode(rate),encode(mark),source]
        with self.shared.transaction(), localcontext() as ctx:
            ctx.prec = 120
            self._check();fingerprint, previous = self._prior(operation,data)
            if previous is not None:return previous
            p = self.db.execute('SELECT * FROM cross_positions WHERE symbol=? AND id=?',(symbol,position_id)).fetchone()
            if not p:raise ValueError('Funding position unknown')
            if not p['opened_ms'] <= stamp <= int(now*1000):raise ValueError('Funding outside position lifetime')
            prior = self.db.execute('SELECT revision,value FROM cross_funding WHERE position_id=? AND funding_time=? AND rate_type=? ORDER BY revision DESC LIMIT 1',(position_id,stamp,rate_type)).fetchone()
            from .cross_funding_tracker import CrossFundingTracker
            tracker=object.__new__(CrossFundingTracker);tracker.book=self;tracker.shared=self.shared;tracker.db=self.db
            cursor=tracker._check_cursor(position_id)
            if stamp!=cursor['next_due'] and not (stamp<cursor['next_due'] and prior):
                raise ValueError('Funding does not match recorded deadline')
            if revision != (prior['revision']+1 if prior else 0):raise ValueError('Funding revision conflict')
            self.db.execute('UPDATE cross_quotes SET funding_complete=0')
            value = amount(p['quantity'])*mark*rate*(-1 if p['direction']=='LONG' else 1)
            delta = value-(amount(prior['value']) if prior else 0)
            details = {'symbol':symbol,'position_id':position_id,'time':stamp,'rate_type':rate_type,'revision':revision,'rate':encode(rate),'mark':encode(mark),'value':encode(value),'source':source}
            postings = [(WALLET+':funding','USDT',delta),('external:cross_funding','USDT',-delta)] if delta else []
            self.ledger._post('cross:funding:'+operation,'CROSS_FUNDING' if postings else 'FINANCIAL_METADATA',details,postings,created=now)
            self.db.execute('INSERT INTO cross_funding VALUES(?,?,?,?,?,?,?,?,?)',(position_id,symbol,stamp,rate_type,revision,encode(rate),encode(mark),encode(value),source))
            self.db.execute('UPDATE cross_positions SET funding=? WHERE symbol=?',(encode(amount(p['funding'])+delta),symbol))
            result={'value':encode(value),'delta':encode(delta)}
            self._anchor(operation);self._check();self._remember(operation,fingerprint,result)
            return result

    def close(self, operation, symbol, position_id, quantity, quotes, *, now=None, liquidate=False):
        now = time.time() if now is None else now
        if symbol not in SYMBOLS or type(liquidate) is not bool:raise ValueError('Invalid cross close')
        quantity=amount(quantity,positive=True)
        data=['close',symbol,position_id,encode(quantity),liquidate]
        with self.shared.transaction(), localcontext() as ctx:
            ctx.prec=120
            self._check();fingerprint,previous=self._prior(operation,data)
            if previous is not None:return previous
            marks=self._current_marks(quotes,now);risk=self._risk(marks)
            p=next((p for p in risk['positions'] if p['symbol']==symbol and p['id']==position_id),None)
            if not p or amount(p['quantity'])!=quantity:raise ValueError('Stale cross position')
            if liquidate:
                if risk['buffer']>0:raise ValueError('Cross liquidation threshold not reached')
                targets=risk['positions']
            else:
                if risk['buffer']<=0:raise ValueError('Cross requires total liquidation')
                targets=[p]
            results=[]
            for item in targets:
                mark=marks[item['symbol']];q=amount(item['quantity']);margin=amount(item['margin']);funding=amount(item['funding'])
                direction=Decimal(1) if item['direction']=='LONG' else Decimal(-1)
                fill=mark*(1-direction*SLIP_BPS/10000)
                gross=(fill-amount(item['entry']))*q*direction;fee=q*fill*FEE_BPS/10000
                details={'symbol':item['symbol'],'position_id':item['id'],'quantity':item['quantity'],'gross':encode(gross),'exit_fee':encode(fee),'funding':encode(funding),'liquidation':liquidate}
                posts=[(WALLET+':margin','USDT',-margin),(WALLET+':free','USDT',margin)]
                for a,b,v in [(WALLET+':free',WALLET+':funding',funding),(WALLET+':free','external:cross_pnl',gross),('external:cross_fees',WALLET+':free',fee)]:
                    if v:posts.extend([(a,'USDT',v),(b,'USDT',-v)])
                self.ledger._post('cross:close:'+operation+':'+item['symbol'],'CROSS_CLOSE',details,posts,created=now)
                self.db.execute('DELETE FROM cross_positions WHERE symbol=?',(item['symbol'],));results.append(details)
            deficit=-self.ledger.balance(WALLET+':free')
            if deficit>0:
                if not liquidate:raise ValueError('Insufficient cash to settle selected position')
                self.ledger._post('cross:adjustment:'+operation,'CROSS_LIQUIDATION_ADJUSTMENT',{'quantity':encode(deficit)},[(WALLET+':free','USDT',deficit),('external:cross_liquidation_adjustment','USDT',-deficit)],created=now)
            result={'closed':results,'adjustment':encode(max(Decimal(0),deficit))}
            self._anchor(operation);self._check();self._remember(operation,fingerprint,result)
            return result
