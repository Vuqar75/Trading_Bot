"""Offline margin repayment confirmations; not yet wired to Telegram."""
import hashlib
import json
import secrets
import time

from .margin_interest_ledger import MarginInterestLedger
from .paper_wallet_ledger import amount, encode


class MarginRepayProposals:
    def __init__(self,ledger,*,loans=None):
        if loans is not None and (not isinstance(loans,MarginInterestLedger) or loans.ledger is not ledger):
            raise ValueError('Margin loan ledger mismatch')
        self.ledger=ledger;self.db=ledger.db;self.loans=loans or MarginInterestLedger(ledger)
        with ledger.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS margin_repay_proposals(id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,loan_id TEXT NOT NULL,asset TEXT NOT NULL,quantity TEXT NOT NULL,principal TEXT NOT NULL,interest TEXT NOT NULL,expires_ms INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('OPEN','DONE','CANCELED')),result TEXT)")

    def propose(self,request_id,loan_id,quantity,*,now_ms,ttl_ms=120000,clock_ms=None,request_expires_ms=None):
        now_ms=self.loans._time(now_ms);quantity=amount(quantity,positive=True)
        if not isinstance(request_id,str) or not request_id or not isinstance(loan_id,str) or not loan_id or type(ttl_ms) is not int or not 0<ttl_ms<=120000:
            raise ValueError('Invalid repayment request')
        fingerprint=hashlib.sha256(json.dumps([loan_id,encode(quantity)],separators=(',',':')).encode()).hexdigest()
        with self.ledger.transaction():
            old=self.db.execute('SELECT * FROM margin_repay_proposals WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise ValueError('Conflicting repayment request')
                return dict(old)
            if clock_ms is not None:now_ms=self.loans._time(clock_ms())
            if request_expires_ms is not None:
                if type(request_expires_ms) is not int or now_ms>=request_expires_ms:raise ValueError('Repayment request expired')
                ttl_ms=min(ttl_ms,request_expires_ms-now_ms)
            self.loans._check();self.loans.accrue(loan_id,now_ms=now_ms)
            loan=self.db.execute('SELECT * FROM margin_loans WHERE id=?',(loan_id,)).fetchone()
            if not loan or loan['status']!='OPEN':raise ValueError('Loan is not open')
            principal=amount(loan['principal']);interest=amount(loan['interest'])
            if quantity>principal+interest:raise ValueError('Repayment exceeds debt')
            if self.ledger.balance('margin_cross:free',loan['asset'])<quantity:raise ValueError('Insufficient free asset')
            token='R-'+secrets.token_hex(8)
            self.db.execute('INSERT INTO margin_repay_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (token,request_id,fingerprint,loan_id,loan['asset'],encode(quantity),encode(principal),encode(interest),now_ms+ttl_ms,'OPEN',None))
            return dict(self.db.execute('SELECT * FROM margin_repay_proposals WHERE id=?',(token,)).fetchone())

    def confirm(self,token,*,now_ms=None,clock_ms=None):
        if not isinstance(token,str) or not token.startswith('R-'):raise ValueError('Not a repayment confirmation')
        with self.ledger.transaction():
            # Re-read time after acquiring the SQLite write lock: waiting for
            # a competing process must not extend the confirmation deadline.
            now_ms=self.loans._time(now_ms if now_ms is not None else (clock_ms() if clock_ms else int(time.time()*1000)))
            row=self.db.execute('SELECT * FROM margin_repay_proposals WHERE id=?',(token,)).fetchone()
            if not row:raise ValueError('Unknown repayment confirmation')
            if row['status']=='DONE':return json.loads(row['result'])
            if row['status']!='OPEN' or now_ms>=row['expires_ms']:raise ValueError('Repayment confirmation expired or canceled')
            self.loans._check();self.loans.accrue(row['loan_id'],now_ms=now_ms)
            loan=self.db.execute('SELECT * FROM margin_loans WHERE id=?',(row['loan_id'],)).fetchone()
            if (not loan or loan['status']!='OPEN' or loan['asset']!=row['asset'] or
                amount(loan['principal'])!=amount(row['principal']) or amount(loan['interest'])!=amount(row['interest'])):
                raise ValueError('Loan changed; request a new repayment confirmation')
            result=self.loans.repay('proposal:'+token,row['loan_id'],row['quantity'],now_ms=now_ms)
            self.db.execute("UPDATE margin_repay_proposals SET status='DONE',result=? WHERE id=?",(json.dumps(result,sort_keys=True),token))
            return result

    def cancel(self,token,*,now_ms=None,clock_ms=None):
        if not isinstance(token,str) or not token.startswith('R-'):raise ValueError('Not a repayment confirmation')
        with self.ledger.transaction():
            now_ms=self.loans._time(now_ms if now_ms is not None else (clock_ms() if clock_ms else int(time.time()*1000)))
            row=self.db.execute('SELECT status,expires_ms FROM margin_repay_proposals WHERE id=?',(token,)).fetchone()
            if row and row['status']=='CANCELED':return
            if not row or row['status']!='OPEN' or now_ms>=row['expires_ms']:raise ValueError('Repayment confirmation expired or completed')
            self.db.execute("UPDATE margin_repay_proposals SET status='CANCELED' WHERE id=?",(token,))
