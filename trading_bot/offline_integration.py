"""Offline integration fixture. Never wire this module to real exchange or Telegram."""
import hmac
import os
import uuid
from decimal import Decimal, localcontext
from fractions import Fraction
from .telegram_inbox import Inbox, Update
from .order_journal import OrderJournal, OfflineOrderCoordinator, DispatchBlocked
from .order_recovery import _mark, recover


class OfflineIntegration:
    """Two test SQLite files in one attached rollback-journal transaction.

    Single local filesystem and one process owner are prerequisites. This is not
    a live adapter, sender authentication, process fencing or disaster recovery.
    """
    def __init__(self, inbox_path, order_path, *, owner, fault=lambda p:None):
        if not isinstance(owner,str) or not owner: raise ValueError('Synthetic owner required')
        self.owner=owner; self.fault=fault
        self.journal=OrderJournal(order_path)
        try:
            if not _mark(self.journal,'RECOVERY_INCOMPLETE'): raise DispatchBlocked('Accounting requires review')
            self.inbox=Inbox(inbox_path)
            try:
                if os.stat(inbox_path).st_dev != os.stat(order_path).st_dev: raise ValueError('Same local filesystem required')
                if self.inbox.db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete': raise ValueError('Rollback journal required')
                if self.journal.db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete': raise ValueError('Rollback journal required')
                self.inbox.db.execute('ATTACH DATABASE ? AS orders',(str(order_path),))
                self.inbox.db.execute('PRAGMA orders.synchronous=FULL')
            except BaseException:
                self.inbox.close(); raise
        except BaseException:
            self.journal.close(); raise
    def close(self): self.inbox.close(); self.journal.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    def receive(self, update_id, sender, command='',target='',version=0):
        """Synthetic owner check: sender identity is never persisted or logged."""
        if not isinstance(sender,str): raise ValueError('Invalid synthetic sender')
        allowed=hmac.compare_digest(sender,self.owner)
        update=Update(update_id,True,command,target,version) if allowed else Update(update_id,False)
        self.inbox.receive_batch([update])

    def process_close(self):
        """Atomically link inbox update, close dedup, order intent and outbox."""
        db=self.inbox.db
        with self.inbox.transaction():
            row=db.execute("SELECT * FROM updates WHERE status='PENDING' ORDER BY update_id LIMIT 1").fetchone()
            if row is None:return None
            if row['command'] != 'CLOSE': raise ValueError('Only CLOSE is integrated in this fixture')
            state=db.execute('SELECT position,position_version FROM inbox_state').fetchone()
            if (row['target'],row['version']) != tuple(state):
                result='STALE_POSITION'
            else:
                exists=db.execute('SELECT update_id FROM closing_intents WHERE target=? AND version=?',(row['target'],row['version'])).fetchone()
                if exists:
                    result='CLOSE_ALREADY_PENDING'
                else:
                    acct=db.execute('SELECT base_balance FROM orders.ledger_account WHERE singleton=1').fetchone()
                    if acct is None: raise ValueError('Ledger required')
                    quantity=Fraction(acct[0])
                    if quantity <= 0: result='NO_POSITION'
                    else:
                        with localcontext() as ctx:
                            ctx.prec=80
                            q=format(Decimal(quantity.numerator)/Decimal(quantity.denominator),'f')
                            if '.' in q: q=q.rstrip('0').rstrip('.')
                        if not q or Fraction(q)!=quantity: raise ValueError('Position cannot be represented exactly')
                        from .order_journal import Request
                        request=Request('SELL',quantity=q)
                        key='close-'+row['target']+'-'+str(row['version'])
                        if len(key)>100:raise ValueError('Fixture key too long')
                        intent_id=uuid.uuid4().hex; client_id='tb'+uuid.uuid4().hex
                        now=self.journal.clock_ms()
                        db.execute("INSERT INTO orders.order_intents(intent_id,namespace,business_key,client_order_id,symbol,side,quantity,quote_quantity,state,revision,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?, 'PREPARED',0,?,?)",(intent_id,'synthetic-close',key,client_id,request.symbol,request.side,request.quantity,request.quote_quantity,now,now))
                        self.fault('intent_written')
                        db.execute("INSERT INTO orders.order_events(intent_id,revision,from_state,to_state,reason,ts_ms) VALUES (?,0,NULL,'PREPARED','INTENT_PREPARED',?)",(intent_id,now))
                        db.execute('INSERT INTO closing_intents VALUES(?,?,?)',(row['target'],row['version'],row['update_id']))
                        result='CLOSE_INTENT_CREATED'
            db.execute("UPDATE updates SET status='DONE',result=? WHERE update_id=?",(result,row['update_id']))
            db.execute("INSERT INTO outbox VALUES(?,?,'PENDING')",(row['update_id'],result))
            self.fault('command_before_commit')
        self.fault('command_committed')
        return row['update_id']

    def close_intent(self,target,version):
        key='close-'+target+'-'+str(version)
        row=self.journal.db.execute("SELECT intent_id FROM order_intents WHERE namespace='synthetic-close' AND business_key=?",(key,)).fetchone()
        return self.journal.get(row[0]) if row else None

    def dispatch(self,intent_id,exchange):
        return OfflineOrderCoordinator(self.journal,exchange).dispatch(intent_id)

    def recover(self,exchange):
        return recover(self.journal,exchange)
