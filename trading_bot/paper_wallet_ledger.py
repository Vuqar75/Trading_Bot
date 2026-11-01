"""Offline prototype of a double-entry paper ledger.

This is intentionally not wired to trading, Telegram, or the running VM.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext

WALLETS = ('reserve', 'spot', 'futures_isolated', 'futures_cross')
TRADING = ('spot', 'futures_isolated', 'futures_cross')
ASSETS = ('USDT', 'BTC')


def valid_asset(asset):
    return isinstance(asset,str) and re.fullmatch(r'[A-Z0-9]{2,12}',asset) is not None


def amount(value, *, positive=False):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid amount') from None
    if not n.is_finite() or abs(n) >= Decimal('1e20') or n.as_tuple().exponent < -60 or len(n.as_tuple().digits) > 80 or (positive and n <= 0):
        raise ValueError('Amount outside supported precision or range')
    return n


def encode(n):
    return format(n, 'f')


class PaperWalletLedger:
    def __init__(self, path, *, fault=lambda point: None, timeout=5):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=timeout, uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.fault = fault
        with self.transaction():
            names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            expected = {'ledger_meta','wallet_state','ledger_events','ledger_postings','transfer_proposals'}
            if names and names != expected:
                raise ValueError('Use a dedicated paper ledger database')
            self.db.execute('CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS wallet_state(wallet TEXT PRIMARY KEY, paused INTEGER NOT NULL CHECK(paused IN (0,1)))')
            self.db.execute('CREATE TABLE IF NOT EXISTS ledger_events(id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL, fingerprint TEXT NOT NULL, details TEXT NOT NULL, posting_count INTEGER NOT NULL CHECK(posting_count>=0))')
            self.db.execute('CREATE TABLE IF NOT EXISTS ledger_postings(event_id TEXT NOT NULL REFERENCES ledger_events(id), ordinal INTEGER NOT NULL, account TEXT NOT NULL, asset TEXT NOT NULL, amount TEXT NOT NULL, PRIMARY KEY(event_id,ordinal))')
            self.db.execute("CREATE TABLE IF NOT EXISTS transfer_proposals(id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, source TEXT NOT NULL, destination TEXT NOT NULL, quantity TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','DONE','CANCELED')), event_id TEXT, source_after TEXT NOT NULL, destination_after TEXT NOT NULL)")
            for wallet in WALLETS:
                self.db.execute('INSERT OR IGNORE INTO wallet_state VALUES(?,1)', (wallet,))
            for table in ('ledger_events','ledger_postings'):
                for verb in ('UPDATE','DELETE'):
                    self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable ledger'); END")
            self.db.execute("CREATE TRIGGER IF NOT EXISTS sealed_postings BEFORE INSERT ON ledger_postings WHEN (SELECT count(*) FROM ledger_postings WHERE event_id=NEW.event_id) >= (SELECT posting_count FROM ledger_events WHERE id=NEW.event_id) BEGIN SELECT RAISE(ABORT,'sealed ledger event'); END")

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            self.db.execute('SAVEPOINT paper_ledger_nested')
            try:
                with localcontext() as context:
                    context.prec = 120
                    yield
                self.db.execute('RELEASE SAVEPOINT paper_ledger_nested')
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT paper_ledger_nested')
                self.db.execute('RELEASE SAVEPOINT paper_ledger_nested')
                raise
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            with localcontext() as context:
                context.prec = 120
                yield
            self.fault('before_commit')
            self.db.commit()
            self.fault('after_commit')
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def balance(self, account, asset='USDT'):
        if not valid_asset(asset):
            raise ValueError('Unsupported asset')
        with localcontext() as context:
            context.prec = 120
            return sum((amount(r[0]) for r in self.db.execute('SELECT amount FROM ledger_postings WHERE account=? AND asset=?', (account, asset))), Decimal(0))

    def _post(self, event_id, kind, details, postings, *, created=None):
        if not event_id or (not postings and kind != 'FINANCIAL_METADATA'):
            raise ValueError('Missing event or postings')
        normalized = [(a, asset, encode(amount(value))) for a, asset, value in postings]
        if any(not a or not valid_asset(asset) or amount(value) == 0 for a, asset, value in normalized):
            raise ValueError('Invalid posting')
        for asset in {item[1] for item in normalized}:
            if sum((amount(v) for _, a, v in normalized if a == asset), Decimal(0)) != 0:
                raise ValueError('Unbalanced event')
        payload = json.dumps([kind, details, normalized], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        old = self.db.execute('SELECT fingerprint FROM ledger_events WHERE id=?', (event_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise ValueError('Conflicting event identity')
            return False
        self.db.execute('INSERT INTO ledger_events VALUES(?,?,?,?,?,?)', (event_id, kind, time.time() if created is None else created, fingerprint, json.dumps(details, sort_keys=True),len(normalized)))
        self.db.executemany('INSERT INTO ledger_postings VALUES(?,?,?,?,?)', [(event_id,i,a,asset,value) for i,(a,asset,value) in enumerate(normalized)])
        return True

    def import_snapshot(self, *, spot_cash, spot_btc, futures_cash, futures_margin, funding, position_metadata, source, captured_at):
        """Import exact externally captured balances once; never creates a default balance."""
        vals = [amount(v) for v in (spot_cash, spot_btc, futures_cash, futures_margin, funding)]
        if any(v < 0 for v in vals[:4]) or not source or not isinstance(position_metadata, dict):
            raise ValueError('Invalid opening snapshot')
        spot_cash, spot_btc, futures_cash, futures_margin, funding = vals
        position = position_metadata.get('futures')
        if (position is None and (futures_margin != 0 or funding != 0)) or (position is not None and (not isinstance(position, dict) or amount(position.get('margin', -1)) != futures_margin or amount(position.get('funding', 'NaN')) != funding)):
            raise ValueError('Position, margin and funding disagree')
        spot_position = position_metadata.get('spot')
        if not isinstance(spot_position, dict) or amount(spot_position.get('quantity', 'NaN')) != spot_btc:
            raise ValueError('Spot quantity disagrees with position')
        entry_fee = amount(position['entry_fee']) if position is not None and position.get('entry_fee') is not None else None
        if entry_fee is not None and entry_fee < 0:
            raise ValueError('Invalid futures entry fee')
        details = {'source': source, 'captured_at': captured_at, 'position_metadata': position_metadata}
        postings = []
        for account, asset, value in [('spot:free','USDT',spot_cash),('spot:position','BTC',spot_btc),('futures_isolated:free','USDT',futures_cash),('futures_isolated:margin','USDT',futures_margin),('futures_isolated:funding','USDT',funding)]:
            if value:
                postings += [(account,asset,value),('external:opening',asset,-value)]
        if entry_fee:
            postings += [('futures_isolated:entry_fee','USDT',-entry_fee),('external:opening','USDT',entry_fee)]
        with self.transaction():
            if self.db.execute("SELECT 1 FROM ledger_meta WHERE key='imported'").fetchone():
                raise ValueError('Opening snapshot already imported')
            if postings:
                self._post('opening:1','OPENING',details,postings,created=captured_at)
            else:
                self.db.execute('INSERT INTO ledger_meta VALUES(?,?)', ('empty_import',json.dumps(details,sort_keys=True)))
            self.db.execute('INSERT INTO ledger_meta VALUES(?,?)', ('imported',json.dumps(details,sort_keys=True)))

    def set_paused(self, wallet, paused):
        if wallet not in TRADING or type(paused) is not bool:
            raise ValueError('Invalid trading wallet state')
        with self.transaction():
            self.db.execute('UPDATE wallet_state SET paused=? WHERE wallet=?', (int(paused), wallet))

    def move_reserve(self, event_id, wallet, value, *, release=False):
        """Atomically move free USDT to/from a trading reserve, with stable ID."""
        if wallet not in TRADING or type(release) is not bool:
            raise ValueError('Invalid reserve wallet')
        quantity = amount(value, positive=True)
        source = wallet+(':reserved' if release else ':free')
        target = wallet+(':free' if release else ':reserved')
        details = {'wallet':wallet,'quantity':encode(quantity),'release':release}
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM ledger_meta WHERE key='imported'").fetchone():
                raise ValueError('Opening snapshot required')
            created = self._post(event_id,'RESERVE',details,[(source,'USDT',-quantity),(target,'USDT',quantity)])
            if self.balance(source) < 0:
                raise ValueError('Insufficient free or reserved funds')
            return created

    def _transfer_allowed(self, source, destination, quantity, *, now=None):
        if source not in WALLETS or destination not in WALLETS or source == destination:
            raise ValueError('Unsupported transfer')
        for wallet in (source,destination):
            if wallet in TRADING:
                state=self.db.execute('SELECT paused FROM wallet_state WHERE wallet=?', (wallet,)).fetchone()
                if not state:raise ValueError('Trading wallet not activated')
                if not state[0]:raise ValueError('Trading wallet must be paused')
        if 'futures_cross' in (source,destination):
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_anchor'").fetchone():
                raise ValueError('Cross not activated')
            hold=self.db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()
            if hold and hold[0]=='true':raise ValueError('Deployment hold is active')
            from .cross_paper_book import CrossPaperBook
            book=object.__new__(CrossPaperBook);book.db=self.db;book.ledger=self
            book._check()
            if book.runtime_error():raise ValueError('Cross requires recovery')
            if source=='futures_cross':
                current=time.time() if now is None else now
                book._require_coverage(current)
                marks=book._marks(book.stored_quotes(),current)
                if book._risk(marks)['available'] < quantity:raise ValueError('Insufficient cross collateral')
        if self.balance(source+':free') < quantity:
            raise ValueError('Insufficient confirmed free funds')

    def propose_transfer(self, request_id, source, destination, value, *, now=None, ttl=120):
        quantity = amount(value, positive=True)
        if not request_id or ttl <= 0:
            raise ValueError('Invalid request')
        now = time.time() if now is None else now
        fingerprint = hashlib.sha256(json.dumps([source,destination,encode(quantity)],separators=(',', ':')).encode()).hexdigest()
        with self.transaction():
            old = self.db.execute('SELECT * FROM transfer_proposals WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError('Conflicting request identity')
                return dict(old)
            if not self.db.execute("SELECT 1 FROM ledger_meta WHERE key='imported'").fetchone():
                raise ValueError('Opening snapshot required')
            self._transfer_allowed(source, destination, quantity, now=now)
            token = 'T-'+secrets.token_hex(8)
            source_after = encode(self.balance(source+':free')-quantity)
            destination_free=self.balance(destination+':free')+quantity
            if destination=='futures_cross':
                destination_free-=min(destination_free,-self.balance('futures_cross:liability'))
            destination_after = encode(destination_free)
            self.db.execute('INSERT INTO transfer_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)', (token,request_id,fingerprint,source,destination,encode(quantity),now+ttl,'OPEN',None,source_after,destination_after))
            return {'id':token,'source':source,'destination':destination,'quantity':encode(quantity),'expires':now+ttl,'status':'OPEN',
                    'source_after':source_after,'destination_after':destination_after}

    def confirm_transfer(self, token, *, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute('SELECT * FROM transfer_proposals WHERE id=?', (token,)).fetchone()
            if not row:
                raise ValueError('Unknown transfer confirmation')
            if row['status'] == 'DONE':
                return row['event_id']
            if row['status'] != 'OPEN' or now >= row['expires']:
                raise ValueError('Transfer confirmation expired or canceled')
            quantity = amount(row['quantity'], positive=True)
            self._transfer_allowed(row['source'], row['destination'], quantity, now=now)
            event = 'transfer:'+token
            self._post(event,'TRANSFER',{'source':row['source'],'destination':row['destination'],'quantity':row['quantity']},
                       [(row['source']+':free','USDT',-quantity),(row['destination']+':free','USDT',quantity)],created=now)
            if row['destination']=='futures_cross':
                from .cross_paper_book import CrossPaperBook
                book=object.__new__(CrossPaperBook);book.db=self.db;book.ledger=self
                book._settle_liability('transfer:'+token)
            self.db.execute("UPDATE transfer_proposals SET status='DONE',event_id=? WHERE id=?", (event,token))
            return event

    def cancel_transfer(self, token, *, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute('SELECT status,expires FROM transfer_proposals WHERE id=?', (token,)).fetchone()
            if row and row['status']=='CANCELED':
                return
            if not row or row['status'] != 'OPEN' or now >= row['expires']:
                raise ValueError('Transfer proposal not open')
            self.db.execute("UPDATE transfer_proposals SET status='CANCELED' WHERE id=?", (token,))

    def reconcile(self):
        with localcontext() as context:
            context.prec = 120
            totals = {asset:Decimal(0) for asset in ASSETS}
            bad = []
            for event in self.db.execute('SELECT id,kind,details,fingerprint,posting_count FROM ledger_events'):
                sums = {}
                rows = self.db.execute('SELECT account,asset,amount FROM ledger_postings WHERE event_id=? ORDER BY ordinal', (event[0],)).fetchall()
                for row in rows:
                    sums[row['asset']] = sums.get(row['asset'],Decimal(0)) + amount(row['amount'])
                payload = json.dumps([event['kind'],json.loads(event['details']),[tuple(r) for r in rows]],sort_keys=True,separators=(',', ':'),ensure_ascii=False)
                if len(rows) != event['posting_count'] or any(sums.values()) or hashlib.sha256(payload.encode()).hexdigest()!=event['fingerprint']:
                    bad.append(event[0])
                for asset,value in sums.items():totals[asset] = totals.get(asset,Decimal(0))+value
            negatives = [wallet+':'+bucket for wallet in WALLETS for bucket in ('free','reserved') if self.balance(wallet+':'+bucket) < 0]
            broken_fk = bool(self.db.execute('PRAGMA foreign_key_check').fetchone())
            return {'balanced':not bad and not any(totals.values()) and not negatives and not broken_fk,
                    'invalid_events':bad,'negative_accounts':negatives,'foreign_key_error':broken_fk,'totals':{k:encode(v) for k,v in totals.items()}}
