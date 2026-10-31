"""Offline integration fixture. Never wire this module to real exchange or Telegram."""
import hmac
import hashlib
import json
import os
import uuid
from decimal import Decimal, localcontext
from fractions import Fraction
from .telegram_inbox import Inbox, Update
from .order_journal import OrderJournal, OfflineOrderCoordinator, DispatchBlocked
from .order_recovery import _mark, recover


def retry_key(target,version,update_id):
    identity=json.dumps([target,version,update_id],separators=(',',':'),ensure_ascii=True)
    return 'close-r-'+hashlib.sha256(identity.encode('ascii')).hexdigest()


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
                self.inbox.db.execute("CREATE TABLE IF NOT EXISTS orders.close_reservations(intent_id TEXT PRIMARY KEY, target TEXT NOT NULL, version INTEGER NOT NULL, quantity TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)))")
                self.inbox.db.execute("CREATE TABLE IF NOT EXISTS orders.close_command_generation(update_id INTEGER PRIMARY KEY, target TEXT NOT NULL, version INTEGER NOT NULL, observed_intent TEXT, observed_state TEXT)")
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
        def snapshot(u):
            if not u.authorized or u.command != 'CLOSE': return
            previous=self.inbox.db.execute('SELECT update_id FROM closing_intents WHERE target=? AND version=?',(u.target,u.version)).fetchone()
            prior=None
            if previous:
                base='close-'+u.target+'-'+str(u.version)
                hashed=retry_key(u.target,u.version,previous[0])
                prior=self.inbox.db.execute("SELECT intent_id,state FROM orders.order_intents WHERE namespace='synthetic-close' AND business_key IN (?,?,?) ORDER BY CASE WHEN business_key=? THEN 0 WHEN business_key=? THEN 1 ELSE 2 END LIMIT 1",(base,hashed,base+'-r'+str(previous[0]),hashed,base+'-r'+str(previous[0]))).fetchone()
            self.inbox.db.execute('INSERT INTO orders.close_command_generation VALUES(?,?,?,?,?)',(u.update_id,u.target,u.version,prior['intent_id'] if prior else None,prior['state'] if prior else None))
        self.inbox.receive_batch([update],on_new=snapshot)

    def process_close(self):
        """Atomically link inbox update, close dedup, order intent and outbox."""
        db=self.inbox.db
        with self.inbox.transaction():
            row=db.execute("SELECT * FROM updates WHERE status='PENDING' ORDER BY update_id LIMIT 1").fetchone()
            if row is None:return None
            if row['command'] != 'CLOSE':
                cmd=row['command']
                result='ENTRIES_ALLOWED' if cmd == 'RESUME' else 'ENTRIES_PAUSED'
                db.execute('UPDATE inbox_state SET paused=? WHERE singleton=1',(int(cmd != 'RESUME'),))
                db.execute("UPDATE updates SET status='DONE',result=? WHERE update_id=?",(result,row['update_id']))
                db.execute("INSERT INTO outbox VALUES(?,?,'PENDING')",(row['update_id'],result))
                return row['update_id']
            state=db.execute('SELECT position,position_version FROM inbox_state').fetchone()
            if (row['target'],row['version']) != tuple(state):
                result='STALE_POSITION'
            else:
                exists=db.execute('SELECT update_id FROM closing_intents WHERE target=? AND version=?',(row['target'],row['version'])).fetchone()
                key='close-'+row['target']+'-'+str(row['version'])
                if exists:
                    prior=db.execute("SELECT intent_id,state FROM orders.order_intents WHERE namespace='synthetic-close' AND business_key IN (?,?,?) ORDER BY CASE WHEN business_key=? THEN 0 WHEN business_key=? THEN 1 ELSE 2 END LIMIT 1",(key,retry_key(row['target'],row['version'],exists['update_id']),key+'-r'+str(exists['update_id']),retry_key(row['target'],row['version'],exists['update_id']),key+'-r'+str(exists['update_id']))).fetchone()
                    captured=db.execute('SELECT observed_intent,observed_state FROM orders.close_command_generation WHERE update_id=?',(row['update_id'],)).fetchone()
                    retry = prior is not None and prior['state']=='REJECTED' and captured is not None and captured['observed_state']=='REJECTED' and captured['observed_intent']==prior['intent_id']
                else: retry=False
                if exists and not retry:
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
                        if retry: key = retry_key(row['target'],row['version'],row['update_id'])
                        if len(key)>100:raise ValueError('Fixture key too long')
                        intent_id=uuid.uuid4().hex; client_id='tb'+uuid.uuid4().hex
                        now=self.journal.clock_ms()
                        db.execute("INSERT INTO orders.order_intents(intent_id,namespace,business_key,client_order_id,symbol,side,quantity,quote_quantity,state,revision,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?, 'PREPARED',0,?,?)",(intent_id,'synthetic-close',key,client_id,request.symbol,request.side,request.quantity,request.quote_quantity,now,now))
                        self.fault('intent_written')
                        db.execute("INSERT INTO orders.order_events(intent_id,revision,from_state,to_state,reason,ts_ms) VALUES (?,0,NULL,'PREPARED','INTENT_PREPARED',?)",(intent_id,now))
                        db.execute('INSERT INTO closing_intents VALUES(?,?,?) ON CONFLICT(target,version) DO UPDATE SET update_id=excluded.update_id',(row['target'],row['version'],row['update_id']))
                        result='CLOSE_INTENT_CREATED'
            db.execute("UPDATE updates SET status='DONE',result=? WHERE update_id=?",(result,row['update_id']))
            db.execute("INSERT INTO outbox VALUES(?,?,'PENDING')",(row['update_id'],result))
            self.fault('command_before_commit')
        self.fault('command_committed')
        return row['update_id']

    def close_intent(self,target,version):
        key='close-'+target+'-'+str(version)
        update=self.inbox.db.execute('SELECT update_id FROM closing_intents WHERE target=? AND version=?',(target,version)).fetchone()
        if update is not None:
            retry=self.journal.db.execute("SELECT intent_id FROM order_intents WHERE namespace='synthetic-close' AND business_key=?",(retry_key(target,version,update[0]),)).fetchone()
            if retry: return self.journal.get(retry[0])
            legacy=self.journal.db.execute("SELECT intent_id FROM order_intents WHERE namespace='synthetic-close' AND business_key=?",(key+'-r'+str(update[0]),)).fetchone()
            if legacy: return self.journal.get(legacy[0])
        row=self.journal.db.execute("SELECT intent_id FROM order_intents WHERE namespace='synthetic-close' AND business_key=?",(key,)).fetchone()
        return self.journal.get(row[0]) if row else None

    def _claim(self,intent_id):
        db=self.inbox.db
        with self.inbox.transaction():
            row=db.execute('SELECT * FROM orders.order_intents WHERE intent_id=?',(intent_id,)).fetchone()
            if row is None or row['state'] != 'PREPARED': raise DispatchBlocked('Intent not prepared')
            if db.execute('SELECT blocked FROM orders.ledger_control').fetchone()[0]: raise DispatchBlocked('Recovery required')
            if row['side']=='BUY':
                if db.execute('SELECT paused FROM inbox_state').fetchone()[0]: raise DispatchBlocked('Entries paused')
                if db.execute('SELECT 1 FROM orders.close_reservations WHERE active=1').fetchone(): raise DispatchBlocked('Exit unresolved')
                if db.execute("SELECT 1 FROM orders.order_intents WHERE state IN ('SUBMITTING','UNKNOWN')").fetchone(): raise DispatchBlocked('Order unresolved')
                import json
                for buy in db.execute("SELECT intent_id,quantity,exchange_status FROM orders.order_intents WHERE side='BUY' AND state='ACKNOWLEDGED'"):
                    if buy['exchange_status']!='FILLED' or buy['quantity'] is None: raise DispatchBlocked('Previous entry unresolved')
                    total=sum((Fraction(json.loads(f[0])['quantity']) for f in db.execute('SELECT payload FROM orders.ledger_fills WHERE intent_id=?',(buy['intent_id'],))),Fraction(0))
                    if total != Fraction(buy['quantity']): raise DispatchBlocked('Previous entry not accounted')
            else:
                if row['namespace'] != 'synthetic-close': raise DispatchBlocked('Exit lacks position binding')
                if db.execute("SELECT 1 FROM orders.order_intents WHERE side='BUY' AND (state IN ('SUBMITTING','UNKNOWN') OR (state='ACKNOWLEDGED' AND exchange_status != 'FILLED'))").fetchone(): raise DispatchBlocked('Entry can still change position')
                binding=next((c for c in db.execute('SELECT target,version,update_id FROM closing_intents') if row['business_key'] in ('close-'+c['target']+'-'+str(c['version']),retry_key(c['target'],c['version'],c['update_id']),'close-'+c['target']+'-'+str(c['version'])+'-r'+str(c['update_id']))),None)
                state=db.execute('SELECT position,position_version FROM inbox_state').fetchone()
                if binding is None or (binding['target'],binding['version'])!=tuple(state): raise DispatchBlocked('Position changed or unknown')
                balance=Fraction(db.execute('SELECT base_balance FROM orders.ledger_account').fetchone()[0])
                if Fraction(row['quantity']) != balance or balance<=0: raise DispatchBlocked('Position quantity changed or unknown')
                if db.execute('SELECT 1 FROM orders.close_reservations WHERE active=1').fetchone(): raise DispatchBlocked('Existing exit reserved')
                if db.execute("SELECT 1 FROM orders.order_intents WHERE side='SELL' AND intent_id != ? AND (state IN ('SUBMITTING','UNKNOWN') OR (state='ACKNOWLEDGED' AND exchange_status != 'FILLED'))",(intent_id,)).fetchone(): raise DispatchBlocked('Previous exit unresolved')
                # A terminal-looking status alone does not establish settled fills.
                import json
                for buy in db.execute("SELECT intent_id,quantity FROM orders.order_intents WHERE side='BUY' AND state='ACKNOWLEDGED' AND exchange_status='FILLED'"):
                    if buy['quantity'] is None: raise DispatchBlocked('Entry totals not proven')
                    accounted=sum((Fraction(json.loads(f[0])['quantity']) for f in db.execute('SELECT payload FROM orders.ledger_fills WHERE intent_id=?',(buy['intent_id'],))), Fraction(0))
                    if accounted != Fraction(buy['quantity']): raise DispatchBlocked('Entry not settled')
                for previous in db.execute("SELECT intent_id,quantity FROM orders.order_intents WHERE side='SELL' AND intent_id != ? AND state='ACKNOWLEDGED' AND exchange_status='FILLED'",(intent_id,)):
                    accounted=sum((Fraction(json.loads(f[0])['quantity']) for f in db.execute('SELECT payload FROM orders.ledger_fills WHERE intent_id=?',(previous['intent_id'],))), Fraction(0))
                    if accounted != Fraction(previous['quantity']): raise DispatchBlocked('Previous exit not settled')
                db.execute('INSERT INTO orders.close_reservations VALUES(?,?,?,?,1)',(intent_id,binding['target'],binding['version'],row['quantity']))
                self.fault('reservation_written')
            now=self.journal.clock_ms()
            db.execute("UPDATE orders.order_intents SET state='SUBMITTING',revision=revision+1,updated_ms=? WHERE intent_id=? AND revision=?",(now,intent_id,row['revision']))
            db.execute("INSERT INTO orders.order_events(intent_id,revision,from_state,to_state,reason,ts_ms) VALUES(?,?,'PREPARED','SUBMITTING','SUBMIT_STARTED',?)",(intent_id,row['revision']+1,now))
            self.fault('claim_before_commit')
        self.fault('claim_committed')
        return self.journal.get(intent_id)

    def dispatch(self,intent_id,exchange):
        return OfflineOrderCoordinator(self.journal,exchange).dispatch(intent_id,claim=self._claim)

    def recover(self,exchange):
        return recover(self.journal,exchange)
