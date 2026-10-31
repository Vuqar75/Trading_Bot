"""Offline ordered ingress. Acceptance requires a committed inbox transaction.

A separate durable spool records the current synthetic update before attempting
SQLite. An incomplete item must be replayed before later updates after restart.
The caller must supply a complete, ordered batch from the source; IDs may skip.
Only one receiver owner is supported; no real Telegram transport is provided.
"""
import json
import hmac
import sqlite3
import threading
import time

class ReceiveStopped(Exception): pass

class SequentialReceiver:
 def __init__(self,app,max_attempts=3,delay=0.01):
  if type(max_attempts) is not int or max_attempts<1 or delay<0:raise ValueError('Invalid retry policy')
  self.app,self.max_attempts,self.delay=app,max_attempts,delay
  self.stopped=False;self._lock=threading.Lock()
  path=app.inbox.db.execute('PRAGMA database_list').fetchone()[2]
  if not path:raise ValueError('Disk-backed inbox required')
  self.spool=sqlite3.connect(path+'.receiver',isolation_level=None,timeout=5)
  self.spool.execute('PRAGMA synchronous=FULL')
  self.spool.execute('CREATE TABLE IF NOT EXISTS receiver_pending(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
 def close(self):self.spool.close()
 def receive_batch(self,updates):
  updates=list(updates)
  if any(a[0]>=b[0] for a,b in zip(updates,updates[1:])):raise ValueError('Updates must be ordered')
  with self._lock:
   if self.stopped:raise ReceiveStopped('Receiver stopped')
   for item in updates:
    if not isinstance(item,(tuple,list)) or not 2<=len(item)<=6:raise ValueError('Invalid update tuple')
    item=list(item)+['','',0,''][len(item)-2:]
    if not isinstance(item[1],str):raise ValueError('Invalid synthetic sender')
    stored=item.copy();stored[1]=hmac.compare_digest(item[1],self.app.owner)
    payload=json.dumps(stored,ensure_ascii=True,separators=(',',':'))
    self.spool.execute('BEGIN IMMEDIATE')
    try:
     existing=self.spool.execute('SELECT payload FROM receiver_pending').fetchone()
     if existing is not None and existing[0]!=payload:raise ReceiveStopped('Replay pending update first')
     if existing is None:self.spool.execute('INSERT INTO receiver_pending VALUES(1,?)',(payload,))
     self.spool.commit()
    except BaseException:self.spool.rollback();self.stopped=True;raise
    for attempt in range(self.max_attempts):
     try:self.app._receive_one(*item);break
     except sqlite3.OperationalError as e:
      if (getattr(e,'sqlite_errorcode',0)&255) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
       self.stopped=True;raise
      if attempt+1==self.max_attempts:
       self.stopped=True;raise ReceiveStopped('SQLite lock; replay pending update') from None
      time.sleep(self.delay)
     except BaseException:self.stopped=True;raise
    self.spool.execute('DELETE FROM receiver_pending WHERE singleton=1 AND payload=?',(payload,))
