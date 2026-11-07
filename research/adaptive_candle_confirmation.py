"""Confirmation of independently received, closed USD-M REST histories."""
import hashlib,json,time
PERIOD=900000
MIN_SEPARATION_MS=10000
MIN_MATURITY_MS=600000

def digest_raw(rows):
 return hashlib.sha256(json.dumps(rows,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def describe(sample):
 if not isinstance(sample,dict) or sample.get('market')!='futures' or sample.get('symbol')!='BTCUSDT' or sample.get('source')!='binance_usdm_klines':raise ValueError('source_identity_invalid')
 rows=sample.get('raw_bars');received=sample.get('received_ms');server=sample.get('server_ms')
 if not isinstance(rows,list) or len(rows)<196 or any(not isinstance(x,list) or len(x)<7 or type(x[0]) is not int or x[0]%PERIOD or x[6]!=x[0]+PERIOD-1 for x in rows) or any(b[0]-a[0]!=PERIOD for a,b in zip(rows,rows[1:])):raise ValueError('candle_gap_or_identity_invalid')
 if type(received) is not int or type(server) is not int or abs(server-received)>5000 or rows[-1][6]>=server:raise ValueError('candle_open_or_clock_invalid')
 if sample.get('skip_bar') is None or rows[-1][0]!=sample['skip_bar'][0]:raise ValueError('candle_boundary_invalid')
 bars=sample.get('bars')
 if not isinstance(bars,list) or len(bars)+1!=len(rows):raise ValueError('parsed_candles_missing')
 try:
  if any(len(parsed)!=5 or parsed[0]!=raw[0] or any(float(raw[j])!=parsed[j] for j in range(1,5)) for parsed,raw in zip(bars+[sample['skip_bar']],rows)):raise ValueError('parsed_candle_mismatch')
 except (TypeError,IndexError):raise ValueError('parsed_candle_mismatch') from None
 return {'digest':digest_raw(rows),'received_ms':received,'server_ms':server,'signal_open_ms':rows[-1][0],'signal_raw':rows[-1]}

def compare(first,second):
 a=describe(first);b=describe(second)
 if a['signal_open_ms']!=b['signal_open_ms']:raise ValueError('candle_boundary_changed')
 if b['received_ms']<=a['received_ms'] or b['server_ms']<a['server_ms']:raise ValueError('observation_time_regressed')
 if b['received_ms']-a['received_ms']<MIN_SEPARATION_MS:return {'state':'awaiting_confirmation','first':a,'second':b}
 if b['received_ms']-(b['signal_open_ms']+PERIOD)<MIN_MATURITY_MS:return {'state':'awaiting_maturity','first':a,'second':b}
 if a['digest']!=b['digest']:return {'state':'preaccept_revision','first':a,'second':b}
 return {'state':'accepted','first':a,'second':b,'accepted_ms':b['received_ms'],'digest':b['digest'],'signal_open_ms':b['signal_open_ms']}

def record_event(db,epoch_id,event,*,clock=lambda:int(time.time()*1000)):
 if type(epoch_id) is not int or not isinstance(event,dict) or event.get('state') not in ('preliminary','awaiting_confirmation','awaiting_maturity','preaccept_revision','accepted','unavailable','gap'):raise ValueError('lifecycle_event_invalid')
 sample=event.get('sample')
 if not isinstance(sample,dict) or type(sample.get('signal_open_ms')) is not int or type(sample.get('received_ms')) is not int or not isinstance(sample.get('digest'),str):raise ValueError('lifecycle_sample_invalid')
 raw=json.dumps(event,sort_keys=True,separators=(',',':'),allow_nan=False);key=hashlib.sha256(raw.encode()).hexdigest()
 needs_durable_stamp=False
 db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS candle_lifecycle(event_sha256 TEXT PRIMARY KEY,epoch_id INTEGER NOT NULL,signal_open_ms INTEGER NOT NULL,state TEXT NOT NULL,observed_ms INTEGER NOT NULL,payload TEXT NOT NULL)')
  db.execute('CREATE TABLE IF NOT EXISTS candle_confirmations(epoch_id INTEGER NOT NULL,signal_open_ms INTEGER NOT NULL,digest TEXT NOT NULL,accepted_ms INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(epoch_id,signal_open_ms,digest))')
  if 'recorded_ms' not in {r[1] for r in db.execute('PRAGMA table_info(candle_confirmations)')}:db.execute('ALTER TABLE candle_confirmations ADD COLUMN recorded_ms INTEGER')
  db.execute('INSERT OR IGNORE INTO candle_lifecycle VALUES(?,?,?,?,?,?)',(key,epoch_id,sample['signal_open_ms'],event['state'],sample['received_ms'],raw))
  if event['state']=='accepted':
   c=event.get('comparison')
   if not isinstance(c,dict) or c.get('state')!='accepted' or c.get('digest')!=sample['digest'] or c.get('accepted_ms')!=sample['received_ms'] or c.get('signal_open_ms')!=sample['signal_open_ms']:raise ValueError('confirmation_invalid')
   a=c.get('first');b=c.get('second')
   if not isinstance(a,dict) or b!=sample or a.get('digest')!=sample['digest'] or a.get('signal_open_ms')!=sample['signal_open_ms'] or type(a.get('received_ms')) is not int or sample['received_ms']-a['received_ms']<MIN_SEPARATION_MS or sample['received_ms']-(sample['signal_open_ms']+PERIOD)<MIN_MATURITY_MS:raise ValueError('confirmation_invalid')
   prior=db.execute('SELECT digest FROM candle_confirmations WHERE epoch_id=? AND signal_open_ms=? AND digest!=? LIMIT 1',(epoch_id,sample['signal_open_ms'],sample['digest'])).fetchone()
   if prior:
    db.execute('INSERT OR IGNORE INTO epoch_conflicts VALUES(?,?,?,?)',(epoch_id,sample['signal_open_ms'],json.dumps({'confirmed_revision':sample},sort_keys=True),sample['received_ms']))
    db.execute('INSERT OR IGNORE INTO candle_lifecycle VALUES(?,?,?,?,?,?)',(hashlib.sha256((key+'contradiction').encode()).hexdigest(),epoch_id,sample['signal_open_ms'],'contradiction_after_acceptance',sample['received_ms'],raw))
   else:
    db.execute('INSERT OR IGNORE INTO candle_confirmations(epoch_id,signal_open_ms,digest,accepted_ms,payload,recorded_ms) VALUES(?,?,?,?,?,NULL)',(epoch_id,sample['signal_open_ms'],sample['digest'],sample['received_ms'],raw));needs_durable_stamp=True
  db.commit()
  if needs_durable_stamp:
   # The timestamp is read only after the acceptance transaction is durable.
   # A crash between the two commits leaves NULL and therefore fails closed.
   recorded=clock()
   if type(recorded) is not int or recorded<sample['received_ms']:raise ValueError('confirmation_recording_time_invalid')
   db.execute('BEGIN IMMEDIATE')
   db.execute('UPDATE candle_confirmations SET recorded_ms=? WHERE epoch_id=? AND signal_open_ms=? AND digest=? AND recorded_ms IS NULL',(recorded,epoch_id,sample['signal_open_ms'],sample['digest']))
   db.commit()
 except BaseException:db.rollback();raise

def confirmed_for_decision(db,epoch_id,candles):
 c=candles.get('warm_confirmation');rows=candles.get('raw_bars');bars=candles.get('bars');skip=candles.get('skip_bar')
 if not isinstance(c,dict) or c.get('state')!='accepted' or not isinstance(rows,list) or not isinstance(bars,list) or not bars or not isinstance(skip,(tuple,list)) or len(rows)!=len(bars)+1:return False
 if c.get('signal_open_ms')!=bars[-1][0] or c.get('digest')!=digest_raw(rows[:-1]) or type(c.get('accepted_ms')) is not int or c['accepted_ms']>=skip[0]+PERIOD:return False
 exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candle_confirmations'").fetchone()
 if not exists or 'recorded_ms' not in {r[1] for r in db.execute('PRAGMA table_info(candle_confirmations)')}:return False
 return bool(db.execute('SELECT 1 FROM candle_confirmations WHERE epoch_id=? AND signal_open_ms=? AND digest=? AND accepted_ms=? AND recorded_ms<?',(epoch_id,c['signal_open_ms'],c['digest'],c['accepted_ms'],skip[0]+PERIOD)).fetchone())

def record_postclose(db,epoch_id,sample,*,clock=lambda:int(time.time()*1000)):
 """Persist a bounded post-close REST check; revised preliminary data are not financial conflicts."""
 info=describe(sample);stamp=info['received_ms'];open_ms=info['signal_open_ms'];raw=json.dumps(info['signal_raw'],separators=(',',':'),allow_nan=False);sha=hashlib.sha256(raw.encode()).hexdigest()
 if not 10000<=stamp-(open_ms+PERIOD)<=120000:raise ValueError('postclose_window_invalid')
 db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS candle_close_observations(event_sha256 TEXT PRIMARY KEY,epoch_id INTEGER NOT NULL,open_ms INTEGER NOT NULL,state TEXT NOT NULL,received_ms INTEGER NOT NULL,server_ms INTEGER,raw_sha256 TEXT NOT NULL,raw_payload TEXT NOT NULL)')
  if 'recorded_ms' not in {r[1] for r in db.execute('PRAGMA table_info(candle_close_observations)')}:db.execute('ALTER TABLE candle_close_observations ADD COLUMN recorded_ms INTEGER')
  provisional=db.execute('SELECT raw_payload,raw_sha256,received_ms FROM epoch_provisional WHERE epoch_id=? AND start_ms=? AND raw_sha256!=\'\' ORDER BY received_ms LIMIT 1',(epoch_id,open_ms)).fetchone()
  def insert(state,received,server,digest,payload):
   key=hashlib.sha256(json.dumps([epoch_id,open_ms,state,received,digest],separators=(',',':')).encode()).hexdigest()
   db.execute('INSERT OR IGNORE INTO candle_close_observations(event_sha256,epoch_id,open_ms,state,received_ms,server_ms,raw_sha256,raw_payload) VALUES(?,?,?,?,?,?,?,?)',(key,epoch_id,open_ms,state,received,server,digest,payload));return key
  if provisional:insert('preliminary',provisional[2],None,provisional[1],provisional[0])
  elif not db.execute("SELECT 1 FROM candle_close_observations WHERE epoch_id=? AND open_ms=? AND state='gap' LIMIT 1",(epoch_id,open_ms)).fetchone():insert('gap',stamp,info['server_ms'],sha,raw)
  accepted=db.execute("SELECT raw_sha256 FROM candle_close_observations WHERE epoch_id=? AND open_ms=? AND state='accepted' ORDER BY received_ms LIMIT 1",(epoch_id,open_ms)).fetchone()
  prior=db.execute("SELECT raw_sha256,received_ms FROM candle_close_observations WHERE epoch_id=? AND open_ms=? AND state IN ('preliminary','awaiting_confirmation') ORDER BY received_ms DESC LIMIT 1",(epoch_id,open_ms)).fetchone()
  if accepted:
   state='accepted' if accepted[0]==sha else 'contradiction_after_acceptance'
   if state=='contradiction_after_acceptance':db.execute('INSERT OR IGNORE INTO epoch_conflicts VALUES(?,?,?,?)',(epoch_id,open_ms,json.dumps({'accepted_raw_revision':info['signal_raw']},sort_keys=True),stamp))
  elif prior and prior[0]==sha and stamp-prior[1]>=MIN_SEPARATION_MS:state='accepted'
  else:state='awaiting_confirmation'
  key=insert(state,stamp,info['server_ms'],sha,raw);db.commit()
  recorded=clock()
  if type(recorded) is not int or recorded<stamp:raise ValueError('confirmation_recording_time_invalid')
  db.execute('BEGIN IMMEDIATE');db.execute('UPDATE candle_close_observations SET recorded_ms=? WHERE event_sha256=? AND recorded_ms IS NULL',(recorded,key));db.commit()
  return {'state':state,'open_ms':open_ms,'received_ms':stamp,'server_ms':info['server_ms'],'recorded_ms':recorded,'digest':sha}
 except BaseException:db.rollback();raise

def record_postclose_gap(db,epoch_id,open_ms,observed_ms,reason):
 if any(type(x) is not int for x in (epoch_id,open_ms,observed_ms)) or observed_ms<open_ms+PERIOD+120000 or reason!='confirmation_window_expired':raise ValueError('gap_invalid')
 db.execute('BEGIN IMMEDIATE')
 try:
  db.execute('CREATE TABLE IF NOT EXISTS candle_close_observations(event_sha256 TEXT PRIMARY KEY,epoch_id INTEGER NOT NULL,open_ms INTEGER NOT NULL,state TEXT NOT NULL,received_ms INTEGER NOT NULL,server_ms INTEGER,raw_sha256 TEXT NOT NULL,raw_payload TEXT NOT NULL)')
  if not db.execute("SELECT 1 FROM candle_close_observations WHERE epoch_id=? AND open_ms=? AND state='accepted'",(epoch_id,open_ms)).fetchone():
   key=hashlib.sha256(json.dumps([epoch_id,open_ms,'gap',reason],separators=(',',':')).encode()).hexdigest()
   db.execute('INSERT OR IGNORE INTO candle_close_observations(event_sha256,epoch_id,open_ms,state,received_ms,server_ms,raw_sha256,raw_payload) VALUES(?,?,?,?,?,?,?,?)',(key,epoch_id,open_ms,'gap',observed_ms,None,'',reason))
  db.commit()
 except BaseException:db.rollback();raise
