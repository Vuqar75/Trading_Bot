"""Offline-only margin loan and hourly interest experiment.

This component is not a trading, risk, transfer or Telegram interface.  Do not
use it for a live worker until those interfaces share the same transaction and
enforce account-level collateral checks. No exchange API is used here.
"""
import hashlib
import json
from decimal import Decimal

from .paper_wallet_ledger import amount, encode

ASSETS = ('USDT', 'BTC', 'ETH')
HOUR_MS = 3_600_000
DEFAULT_RATE = Decimal('0.00001')  # 0.001% per started hour, paper assumption.
WALLET = 'margin_cross'


class MarginInterestLedger:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        with ledger.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_loan_rates(asset TEXT NOT NULL, effective_ms INTEGER NOT NULL, rate TEXT NOT NULL, PRIMARY KEY(asset,effective_ms))')
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_loans(id TEXT PRIMARY KEY, position_id TEXT NOT NULL, asset TEXT NOT NULL, original TEXT NOT NULL, principal TEXT NOT NULL, interest TEXT NOT NULL, opened_ms INTEGER NOT NULL, status TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_interest_periods(loan_id TEXT NOT NULL REFERENCES margin_loans(id), period INTEGER NOT NULL, at_ms INTEGER NOT NULL, principal TEXT NOT NULL, rate TEXT NOT NULL, charge TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES ledger_events(id), PRIMARY KEY(loan_id,period))')
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_loan_operations(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)')
            for table in ('margin_interest_periods', 'margin_loan_rates'):
                for verb in ('UPDATE','DELETE'):
                    self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'immutable margin history'); END")
            for asset in ASSETS:
                self.db.execute('INSERT OR IGNORE INTO margin_loan_rates VALUES(?,?,?)',(asset,0,encode(DEFAULT_RATE)))

    @staticmethod
    def _time(value):
        if type(value) is not int or value < 0:
            raise ValueError('Integer milliseconds required')
        return value

    @staticmethod
    def _fingerprint(parts):
        return hashlib.sha256(json.dumps(parts,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def _prior(self, operation, parts):
        if not isinstance(operation,str) or not operation:
            raise ValueError('Operation identity required')
        fingerprint=self._fingerprint(parts)
        row=self.db.execute('SELECT fingerprint,result FROM margin_loan_operations WHERE id=?',(operation,)).fetchone()
        if row and row['fingerprint'] != fingerprint:
            raise ValueError('Conflicting margin operation identity')
        return fingerprint, json.loads(row['result']) if row else None

    def _remember(self, operation, fingerprint, result):
        self.db.execute('INSERT INTO margin_loan_operations VALUES(?,?,?)',(operation,fingerprint,json.dumps(result,sort_keys=True)))

    def _rate(self, asset, at_ms):
        row=self.db.execute('SELECT rate FROM margin_loan_rates WHERE asset=? AND effective_ms<=? ORDER BY effective_ms DESC LIMIT 1',(asset,at_ms)).fetchone()
        if row is None:raise ValueError('Margin rate is unknown')
        return amount(row['rate'])

    def _check(self):
        if not self.ledger.reconcile()['balanced']:
            raise ValueError('Paper ledger is inconsistent')
        for asset in ASSETS:
            rows=self.db.execute('SELECT principal,interest,status FROM margin_loans WHERE asset=?',(asset,)).fetchall()
            principal=sum((amount(row['principal']) for row in rows),Decimal(0))
            interest=sum((amount(row['interest']) for row in rows),Decimal(0))
            if any(amount(row['principal'])<0 or amount(row['interest'])<0 or row['status'] not in ('OPEN','CLOSED') or (row['status']=='CLOSED' and (amount(row['principal']) or amount(row['interest']))) for row in rows):
                raise ValueError('Invalid margin loan state')
            if self.ledger.balance(WALLET+':principal',asset)!=-principal or self.ledger.balance(WALLET+':interest',asset)!=-interest or self.ledger.balance(WALLET+':free',asset)<0:
                raise ValueError('Margin balances disagree with loans')

    def set_rate(self, asset, rate, *, effective_ms):
        """Append a prospective paper rate without rewriting past periods."""
        effective_ms=self._time(effective_ms)
        rate=amount(rate)
        if asset not in ASSETS or not 0 <= rate < 1:raise ValueError('Invalid paper rate')
        with self.ledger.transaction():
            self._check()
            old=self.db.execute('SELECT rate FROM margin_loan_rates WHERE asset=? AND effective_ms=?',(asset,effective_ms)).fetchone()
            if old:
                if amount(old['rate'])!=rate:raise ValueError('Conflicting rate identity')
                return
            latest=self.db.execute('SELECT MAX(effective_ms) FROM margin_loan_rates WHERE asset=?',(asset,)).fetchone()[0]
            if effective_ms<=latest or self.db.execute('SELECT 1 FROM margin_interest_periods p JOIN margin_loans l ON l.id=p.loan_id WHERE l.asset=? AND p.at_ms>=? LIMIT 1',(asset,effective_ms)).fetchone():
                raise ValueError('Rate would rewrite history')
            self.ledger._post(f'margin:rate:{asset}:{effective_ms}','FINANCIAL_METADATA',{'asset':asset,'effective_ms':effective_ms,'rate':encode(rate)},[],created=effective_ms/1000)
            self.db.execute('INSERT INTO margin_loan_rates VALUES(?,?,?)',(asset,effective_ms,encode(rate)))

    def _accrue(self, loan_id, until_ms):
        row=self.db.execute('SELECT * FROM margin_loans WHERE id=?',(loan_id,)).fetchone()
        if not row:raise ValueError('Unknown loan')
        if until_ms<row['opened_ms']:raise ValueError('Time precedes loan')
        if row['status']=='CLOSED':return []
        principal=amount(row['principal']); interest=amount(row['interest'])
        last=self.db.execute('SELECT MAX(period) FROM margin_interest_periods WHERE loan_id=?',(loan_id,)).fetchone()[0]
        start=0 if last is None else last+1
        end=(until_ms-row['opened_ms'])//HOUR_MS
        if last is not None and end<last:raise ValueError('Interest clock moved backwards')
        if end-start>100_000:raise ValueError('Too many pending interest periods')
        charged=[]
        for period in range(start,end+1):
            when=row['opened_ms']+period*HOUR_MS;rate=self._rate(row['asset'],when);charge=principal*rate
            event=f'margin:interest:{loan_id}:{period}'
            details={'loan_id':loan_id,'period':period,'at_ms':when,'asset':row['asset'],'principal':encode(principal),'rate':encode(rate),'charge':encode(charge)}
            posts=[(WALLET+':interest',row['asset'],-charge),('external:margin_interest',row['asset'],charge)] if charge else []
            self.ledger._post(event,'MARGIN_INTEREST' if posts else 'FINANCIAL_METADATA',details,posts,created=when/1000)
            self.db.execute('INSERT INTO margin_interest_periods VALUES(?,?,?,?,?,?,?)',(loan_id,period,when,encode(principal),encode(rate),encode(charge),event))
            interest+=charge;charged.append(details)
        if charged:self.db.execute('UPDATE margin_loans SET interest=? WHERE id=?',(encode(interest),loan_id))
        return charged

    def borrow(self, operation, loan_id, position_id, asset, quantity, *, now_ms):
        now_ms=self._time(now_ms);quantity=amount(quantity,positive=True)
        if asset not in ASSETS or not isinstance(loan_id,str) or not loan_id or not isinstance(position_id,str) or not position_id:raise ValueError('Invalid loan')
        parts=['borrow',loan_id,position_id,asset,encode(quantity),now_ms]
        with self.ledger.transaction():
            self._check()
            fp,old=self._prior(operation,parts)
            if old is not None:return old
            if self.db.execute('SELECT 1 FROM margin_loans WHERE id=?',(loan_id,)).fetchone():raise ValueError('Loan identity already used')
            details={'loan_id':loan_id,'position_id':position_id,'asset':asset,'quantity':encode(quantity),'opened_ms':now_ms}
            self.ledger._post('margin:borrow:'+operation,'MARGIN_BORROW',details,[(WALLET+':free',asset,quantity),(WALLET+':principal',asset,-quantity)],created=now_ms/1000)
            self.db.execute('INSERT INTO margin_loans VALUES(?,?,?,?,?,?,?,?)',(loan_id,position_id,asset,encode(quantity),encode(quantity),'0',now_ms,'OPEN'))
            self._accrue(loan_id,now_ms)
            result={'loan_id':loan_id,'asset':asset,'principal':encode(quantity)}
            self._remember(operation,fp,result)
            self._check()
            return result

    def accrue(self, loan_id, *, now_ms):
        now_ms=self._time(now_ms)
        with self.ledger.transaction():
            self._check();result=self._accrue(loan_id,now_ms);self._check();return result

    def repay(self, operation, loan_id, quantity, *, now_ms):
        now_ms=self._time(now_ms);quantity=amount(quantity,positive=True)
        parts=['repay',loan_id,encode(quantity),now_ms]
        with self.ledger.transaction():
            self._check()
            fp,old=self._prior(operation,parts)
            if old is not None:return old
            self._accrue(loan_id,now_ms)
            row=self.db.execute('SELECT * FROM margin_loans WHERE id=?',(loan_id,)).fetchone()
            if row['status']!='OPEN':raise ValueError('Loan is closed')
            interest=amount(row['interest']);principal=amount(row['principal']);asset=row['asset']
            if quantity>interest+principal:raise ValueError('Repayment exceeds debt')
            if self.ledger.balance(WALLET+':free',asset)<quantity:raise ValueError('Insufficient free asset')
            paid_interest=min(interest,quantity);paid_principal=quantity-paid_interest
            posts=[(WALLET+':free',asset,-quantity)]
            if paid_interest:posts.append((WALLET+':interest',asset,paid_interest))
            if paid_principal:posts.append((WALLET+':principal',asset,paid_principal))
            details={'loan_id':loan_id,'asset':asset,'quantity':encode(quantity),'interest':encode(paid_interest),'principal':encode(paid_principal),'at_ms':now_ms}
            self.ledger._post('margin:repay:'+operation,'MARGIN_REPAY',details,posts,created=now_ms/1000)
            principal-=paid_principal;interest-=paid_interest
            self.db.execute('UPDATE margin_loans SET principal=?,interest=?,status=? WHERE id=?',(encode(principal),encode(interest),'CLOSED' if not principal and not interest else 'OPEN',loan_id))
            result={'asset':asset,'interest':encode(paid_interest),'principal':encode(paid_principal),'remaining_principal':encode(principal),'remaining_interest':encode(interest)}
            self._remember(operation,fp,result)
            self._check()
            return result
