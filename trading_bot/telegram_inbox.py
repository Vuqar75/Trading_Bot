"""Offline command inbox/outbox prototype; no Telegram transport or credentials.

Synthetic updates contain a sequence number and allowlisted command only.  All
state and order-intent effects are local fixtures, never real bot actions.
"""
import re
import sqlite3
from contextlib import contextmanager, closing
from dataclasses import dataclass


class Conflict(Exception): pass
class DeliveryUncertain(Exception): pass


@dataclass(frozen=True)
class Update:
    update_id: int
    authorized: bool
    command: str = ''
    target: str = ''
    version: int = 0


def _validate(u):
    if not isinstance(u,Update) or type(u.update_id) is not int or not 0 <= u.update_id < 9223372036854775807:
        raise ValueError('Invalid synthetic update')
    if type(u.authorized) is not bool: raise ValueError('Invalid authorization flag')
    if not u.authorized:
        if u.command or u.target or u.version: raise ValueError('Unauthorized updates must be stripped')
        return
    if u.command not in ('PAUSE','STOP','RESUME','CLOSE','RETRY_CLOSE'): raise ValueError('Unsupported simulated command')
    if u.command in ('CLOSE','RETRY_CLOSE'):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',u.target) or type(u.version) is not int or u.version < 1:
            raise ValueError('CLOSE requires an internal position and version')
    elif u.target or u.version: raise ValueError('Unexpected command parameters')


class Inbox:
    def __init__(self,path,*,fault=lambda point:None):
        self.db=sqlite3.connect(path,isolation_level=None,timeout=5)
        self.db.row_factory=sqlite3.Row; self.fault=fault
        try:
            self.db.execute('PRAGMA foreign_keys=ON'); self.db.execute('PRAGMA synchronous=FULL')
            with self.transaction():
                tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if tables and tables != {'inbox_schema','inbox_state','updates','outbox','closing_intents'}:
                    raise ValueError('Dedicated test database required')
                if tables:
                    if self.db.execute('SELECT version FROM inbox_schema').fetchone()[0] != 1: raise ValueError('Unsupported schema')
                else:
                    self.db.execute('CREATE TABLE inbox_schema(version INTEGER NOT NULL)')
                    self.db.execute('INSERT INTO inbox_schema VALUES(1)')
                    self.db.execute("CREATE TABLE inbox_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1), next_offset INTEGER NOT NULL, paused INTEGER NOT NULL, position TEXT NOT NULL, position_version INTEGER NOT NULL)")
                    self.db.execute("INSERT INTO inbox_state VALUES(1,0,1,'',0)")
                    self.db.execute("CREATE TABLE updates(update_id INTEGER PRIMARY KEY, authorized INTEGER NOT NULL, command TEXT NOT NULL, target TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL)")
                    self.db.execute("CREATE TABLE outbox(update_id INTEGER PRIMARY KEY REFERENCES updates(update_id), result TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SENT')))")
                    self.db.execute("CREATE TABLE closing_intents(target TEXT NOT NULL, version INTEGER NOT NULL, update_id INTEGER NOT NULL REFERENCES updates(update_id), PRIMARY KEY(target,version))")
        except BaseException:
            self.db.close(); raise
    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try: yield; self.db.commit()
        except BaseException: self.db.rollback(); raise
    def close(self): self.db.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

    def receive_rejected(self, update_id, fingerprint):
        if type(update_id) is not int or not 0 <= update_id < 9223372036854775807:
            raise ValueError('Invalid synthetic update')
        with self.transaction():
            offset=self.db.execute('SELECT next_offset FROM inbox_state').fetchone()[0]
            row=self.db.execute('SELECT authorized,command,target,version,status FROM updates WHERE update_id=?',(update_id,)).fetchone()
            if row:
                if tuple(row)!=(1,'REJECTED',fingerprint,0,'REJECTED'): raise Conflict('Update payload conflict')
            else:
                if update_id < offset: raise Conflict('Unknown old update; manual review required')
                self.db.execute("INSERT INTO updates VALUES(?,1,'REJECTED',?,0,'REJECTED','INVALID_INPUT')",(update_id,fingerprint))
            self.fault('receive_written')
            self.db.execute('UPDATE inbox_state SET next_offset=? WHERE singleton=1',(max(offset,update_id+1),))
            self.fault('receive_before_commit')
        self.fault('receive_committed')

    def receive_batch(self,updates, on_new=None, on_duplicate=None):
        """Commit complete, already-authenticated fixture batch before advancing offset.

        Transport must prove batches are complete and ordered; this offline prototype
        does not implement Telegram polling, authentication or update-id reset policy.
        """
        updates=list(updates)
        for u in updates: _validate(u)
        if any(a.update_id >= b.update_id for a,b in zip(updates,updates[1:])): raise ValueError('Batch must be strictly ordered')
        with self.transaction():
            offset=self.db.execute('SELECT next_offset FROM inbox_state').fetchone()[0]
            for u in updates:
                row=self.db.execute('SELECT * FROM updates WHERE update_id=?',(u.update_id,)).fetchone()
                record=(int(u.authorized),u.command,u.target,u.version)
                if row:
                    if tuple(row[k] for k in ('authorized','command','target','version')) != record: raise Conflict('Update payload conflict')
                    if on_duplicate is not None: on_duplicate(u)
                    continue
                if u.update_id < offset: raise Conflict('Unknown old update; manual review required')
                self.db.execute("INSERT INTO updates VALUES(?,?,?,?,?,?,?)",(u.update_id,*record,'PENDING' if u.authorized else 'IGNORED',''))
                if on_new is not None: on_new(u)
            self.fault('receive_written')
            if updates: self.db.execute('UPDATE inbox_state SET next_offset=? WHERE singleton=1',(max(offset,updates[-1].update_id+1),))
            self.fault('receive_before_commit')
        self.fault('receive_committed')

    def set_simulated_position(self,target,version):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',target) or type(version) is not int or version < 1: raise ValueError('Invalid fixture position')
        with self.transaction():self.db.execute('UPDATE inbox_state SET position=?,position_version=? WHERE singleton=1',(target,version))

    def process_one(self):
        with self.transaction():
            row=self.db.execute("SELECT * FROM updates WHERE status='PENDING' ORDER BY update_id LIMIT 1").fetchone()
            if row is None:return None
            cmd=row['command']
            if cmd in ('PAUSE','STOP','RESUME'):
                self.db.execute('UPDATE inbox_state SET paused=? WHERE singleton=1',(int(cmd!='RESUME'),))
                result='ENTRIES_ALLOWED' if cmd=='RESUME' else 'ENTRIES_PAUSED'
            else:
                state=self.db.execute('SELECT position,position_version FROM inbox_state').fetchone()
                if (row['target'],row['version']) != tuple(state): result='STALE_POSITION'
                else:
                    cursor=self.db.execute('INSERT OR IGNORE INTO closing_intents VALUES(?,?,?)',(row['target'],row['version'],row['update_id']))
                    result='CLOSE_INTENT_CREATED' if cursor.rowcount else 'CLOSE_ALREADY_PENDING'
            self.fault('effect_written')
            self.db.execute("UPDATE updates SET status='DONE',result=? WHERE update_id=?",(result,row['update_id']))
            self.db.execute("INSERT INTO outbox VALUES(?,?,'PENDING')",(row['update_id'],result))
            self.fault('process_before_commit')
        self.fault('process_committed')
        return row['update_id']

    def send_one(self,simulator):
        if not isinstance(simulator,ResponseSimulator): raise ValueError('Only response simulator permitted')
        with self.transaction():
            row=self.db.execute("SELECT * FROM outbox WHERE status='PENDING' ORDER BY update_id LIMIT 1").fetchone()
            if row is None:return None
            self.db.execute("UPDATE outbox SET status='SENDING' WHERE update_id=?",(row['update_id'],))
            self.fault('send_claim_before_commit')
        self.fault('send_claim_committed')
        try: simulator.send(row['update_id'],row['result'])
        except Exception: raise DeliveryUncertain('Response delivery uncertain') from None
        self.fault('send_returned')
        with self.transaction():
            self.db.execute("UPDATE outbox SET status='SENT' WHERE update_id=? AND status='SENDING'",(row['update_id'],))
            self.fault('send_result_before_commit')
        self.fault('send_result_committed')
        return row['update_id']

    def requeue_uncertain(self):
        """Only after proving no other sender is active; may duplicate responses."""
        with self.transaction():
            self.db.execute("UPDATE outbox SET status='PENDING' WHERE status='SENDING'")


class ResponseSimulator:
    """Dedicated separate SQLite records every send, intentionally no deduplication."""
    def __init__(self,path,scenario='ok'):
        if scenario not in ('ok','timeout_before','timeout_after'): raise ValueError('Invalid scenario')
        self.path,self.scenario=path,scenario
        with closing(sqlite3.connect(path)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and tables != {'response_calls'}: raise ValueError('Dedicated simulator database required')
            db.execute('CREATE TABLE IF NOT EXISTS response_calls(update_id INTEGER,result TEXT)')
    def send(self,update_id,result):
        if self.scenario=='timeout_before':raise TimeoutError('simulated')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO response_calls VALUES(?,?)',(update_id,result)); db.commit()
        if self.scenario=='timeout_after':raise TimeoutError('simulated')
