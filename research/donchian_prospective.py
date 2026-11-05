"""Isolated public-data-only prospective observer. No trade or private API imports."""
import argparse,datetime as dt,fcntl,hashlib,json,os,sqlite3,sys,time,urllib.parse,urllib.request
from decimal import Decimal,ROUND_DOWN
from pathlib import Path

DAY=86400000; WINDOW=300000; HOST='https://data-api.binance.vision'; SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
STEPS={'BTCUSDT':Decimal('.00001'),'ETHUSDT':Decimal('.0001'),'SOLUSDT':Decimal('.001')}
D=Decimal
def utc(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()
def now():return int(time.time()*1000)
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'))
def request(path,params=None):
    url=HOST+path+('?' + urllib.parse.urlencode(params) if params else '')
    for i in range(3):
        try:
            with urllib.request.urlopen(url,timeout=8) as r:return json.loads(r.read())
        except (OSError,ValueError):
            if i==2:raise RuntimeError('public_data_unavailable') from None
            time.sleep(2**i)
def server_time():return int(request('/api/v3/time')['serverTime'])
def public_quote(symbol):
    start=now();before=server_time();raw=request('/api/v3/ticker/price',{'symbol':symbol});after=server_time();received=now()
    p=D(raw['price'])
    if raw['symbol']!=symbol or not p.is_finite() or p<=0 or after<before or received-start>5000 or abs(before-start)>5000 or abs(after-received)>5000:raise ValueError('quote_freshness_unconfirmed')
    return {'symbol':symbol,'price':str(p),'observed_ms':received,'request_ms':start,'server_before_ms':before,'server_after_ms':after}

def parse_days(raw,server,minimum=61):
    out=[]
    for row in raw:
        if not isinstance(row,list) or len(row)<7:raise ValueError('bad_candle')
        t=int(row[0]);end=int(row[6]);o,h,l,c=map(D,row[1:5])
        if t%DAY or end!=t+DAY-1 or not all(x.is_finite() for x in (o,h,l,c)) or not (D(0)<l<=min(o,c)<=max(o,c)<=h):raise ValueError('bad_candle')
        if end>=server:continue
        x={'open_ms':t,'close_ms':end,'open':str(o),'high':str(h),'low':str(l),'close':str(c)}
        out.append(x)
    if len(out)<minimum or any(out[i]['open_ms']+DAY!=out[i+1]['open_ms'] for i in range(len(out)-1)):raise ValueError('incomplete_or_discontinuous_candles')
    return out

def decision(closes,position,level):
    if len(closes)<61:raise ValueError('warmup_incomplete')
    p=list(map(D,closes));mid=(max(p[-60:])+min(p[-60:]))/2
    if position:
        lv=D(level)
        return ('SELL',str(lv)) if p[-1]<lv else ('HOLD',str(max(lv,mid)))
    return ('BUY',str(mid)) if p[-1]>max(p[-61:-1]) else ('HOLD',None)

def connect(path):
    db=sqlite3.connect(path,isolation_level=None,timeout=5);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL');db.row_factory=sqlite3.Row;return db
def meta(db,k):
    r=db.execute('SELECT value FROM meta WHERE key=?',(k,)).fetchone();return json.loads(r[0]) if r else None
def setmeta(db,k,v):db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,canonical(v)))
def init(path,version,start=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);os.chmod(path.parent,0o700)
    if path.exists():raise ValueError('state_already_exists')
    db=connect(path);db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE candles(symbol TEXT,open_ms INTEGER,hash TEXT,first_received_ms INTEGER,payload TEXT,PRIMARY KEY(symbol,open_ms));
CREATE TABLE candle_versions(symbol TEXT,open_ms INTEGER,hash TEXT,received_ms INTEGER,payload TEXT,PRIMARY KEY(symbol,open_ms,hash));
CREATE TABLE accounts(symbol TEXT,scenario TEXT,kind TEXT,state TEXT,PRIMARY KEY(symbol,scenario,kind));
CREATE TABLE decisions(boundary_ms INTEGER,symbol TEXT,scenario TEXT,action TEXT,level TEXT,decided_ms INTEGER,commit_seen_ms INTEGER,candle_hash TEXT,inputs_hash TEXT,version TEXT,PRIMARY KEY(boundary_ms,symbol,scenario));
CREATE TABLE quotes(id TEXT PRIMARY KEY,status TEXT,claimed_ms INTEGER,quote TEXT);
CREATE TABLE events(id TEXT PRIMARY KEY,at_ms INTEGER,payload TEXT);''')
    db.execute('BEGIN IMMEDIATE');start=now() if start is None else start
    for k,v in [('start_ms',start),('next_boundary',((start//DAY)+1)*DAY),('halted',False),('version',version),('last_fetch_ms',None),('error',None)]:setmeta(db,k,v)
    for s in SYMBOLS:
        for scenario in ('base','stress'):
            for kind in ('strategy','hold','cash'):
                db.execute('INSERT INTO accounts VALUES(?,?,?,?)',(s,scenario,kind,canonical({'cash':'1000','qty':'0','cost':'0','level':None,'realized':'0','peak':'1000','max_drawdown':'0'})))
    db.commit();db.close();os.chmod(path,0o600);return start

def state(db,s,scenario,kind):return json.loads(db.execute('SELECT state FROM accounts WHERE symbol=? AND scenario=? AND kind=?',(s,scenario,kind)).fetchone()[0])
def save(db,s,scenario,kind,a):db.execute('UPDATE accounts SET state=? WHERE symbol=? AND scenario=? AND kind=?',(canonical(a),s,scenario,kind))
def event(db,id,ts,payload):db.execute('INSERT INTO events VALUES(?,?,?)',(id,ts,canonical(payload)))
def apply_fill(a,s,scenario,action,level,q):
    p=D(q['price']);slip=D('.0005') if scenario=='base' else D('.001');fee=D('.001');cash=D(a['cash']);qty=D(a['qty'])
    if action=='BUY':
        price=p*(1+slip);qty=((D(10)/price)/STEPS[s]).to_integral_value(rounding=ROUND_DOWN)*STEPS[s];value=qty*price;commission=value*fee
        if value<5 or value+commission>cash:return {'result':'rejected_minimum_or_cash'}
        a.update(cash=str(cash-value-commission),qty=str(qty),cost=str(value+commission),level=level)
    else:
        if qty<=0:raise ValueError('position_missing')
        price=p*(1-slip);value=qty*price;commission=value*fee;real=value-commission-D(a['cost'])
        a.update(cash=str(cash+value-commission),qty='0',cost='0',level=None,realized=str(D(a['realized'])+real))
    return {'result':'filled','side':action,'quantity':str(qty),'fill_price':str(price),'fee':str(commission),'quote':q}

def record_mark(db,s,q):
    setmeta(db,'quote:'+s,q)
    for scenario in ('base','stress'):
        for kind in ('strategy','hold','cash'):
            a=state(db,s,scenario,kind);slip=D('.0005') if scenario=='base' else D('.001')
            equity=D(a['cash'])+D(a['qty'])*D(q['price'])*(1-slip)*D('.999');peak=max(D(a['peak']),equity)
            a['peak']=str(peak);a['max_drawdown']=str(max(D(a['max_drawdown']),(peak-equity)/peak));save(db,s,scenario,kind,a)

def backup(db,path):
    target=str(path)+'.tmp';Path(target).unlink(missing_ok=True);dest=sqlite3.connect(target)
    try:
        db.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup_integrity_failed')
    finally:dest.close()
    os.chmod(target,0o600);os.replace(target,path)

def ingest(db,s,days,received):
    changed=False
    for c in days:
        payload=canonical(c);digest=hashlib.sha256(payload.encode()).hexdigest();old=db.execute('SELECT hash FROM candles WHERE symbol=? AND open_ms=?',(s,c['open_ms'])).fetchone()
        db.execute('INSERT OR IGNORE INTO candle_versions VALUES(?,?,?,?,?)',(s,c['open_ms'],digest,received,payload))
        if old and old['hash']!=digest:changed=True
        if not old:db.execute('INSERT INTO candles VALUES(?,?,?,?,?)',(s,c['open_ms'],digest,received,payload))
    if changed:setmeta(db,'halted',True);setmeta(db,'error','source_history_changed')
    return not changed

def quote_claim(db,id,claimed):
    db.execute('BEGIN IMMEDIATE')
    if db.execute('SELECT 1 FROM quotes WHERE id=?',(id,)).fetchone():db.rollback();return False
    db.execute('INSERT INTO quotes VALUES(?,?,?,NULL)',(id,'claimed',claimed));db.commit();return True

def baseline(db,s,version):
    ident='initial:'+s
    if not quote_claim(db,ident,now()):return
    q=public_quote(s)
    if q['observed_ms']<=meta(db,'start_ms'):raise ValueError('quote_before_start')
    db.execute('BEGIN IMMEDIATE')
    for scenario in ('base','stress'):
        a=state(db,s,scenario,'hold');fill=apply_fill(a,s,scenario,'BUY',None,q);save(db,s,scenario,'hold',a);event(db,ident+':'+scenario,q['observed_ms'],{'version':version,'kind':'buy_and_hold',**fill})
    db.execute('UPDATE quotes SET status=?,quote=? WHERE id=?',('done',canonical(q),ident));record_mark(db,s,q);db.commit()

def apply_decisions(db,s,boundary):
    ds=db.execute('SELECT * FROM decisions WHERE boundary_ms=? AND symbol=? ORDER BY scenario',(boundary,s)).fetchall()
    if not any(d['action'] in ('BUY','SELL') for d in ds):return
    ident=f'{boundary}:{s}'
    if not quote_claim(db,ident,now()):return
    q=public_quote(s)
    if any(d['commit_seen_ms'] is None for d in ds) or q['observed_ms']>boundary+WINDOW or q['request_ms']<=max(d['commit_seen_ms'] for d in ds) or q['request_ms']<boundary:raise ValueError('execution_window_expired')
    db.execute('BEGIN IMMEDIATE')
    for d in ds:
        if d['action']=='HOLD':continue
        a=state(db,s,d['scenario'],'strategy');fill=apply_fill(a,s,d['scenario'],d['action'],d['level'],q);save(db,s,d['scenario'],'strategy',a)
        event(db,ident+':'+d['scenario'],q['observed_ms'],{'boundary_ms':boundary,'signal_decided_ms':d['decided_ms'],'signal_commit_observed_ms':d['commit_seen_ms'],'version':d['version'],'kind':'strategy',**fill})
    db.execute('UPDATE quotes SET status=?,quote=? WHERE id=?',('done',canonical(q),ident));record_mark(db,s,q);db.commit()

def cycle(db,version,fetch=request,quote=public_quote,clock=now):
    global public_quote
    old_quote=public_quote;public_quote=quote
    try:
        if version!=meta(db,'version'):raise ValueError('code_version_changed')
        if db.execute("SELECT 1 FROM quotes WHERE status='claimed'").fetchone():
            db.execute('BEGIN IMMEDIATE');setmeta(db,'halted',True);setmeta(db,'error','unfinished_quote_claim');db.commit();return
        exchange=int(fetch('/api/v3/time')['serverTime']);received=clock()
        if abs(exchange-received)>5000:raise ValueError('clock_skew')
        last_closed=(exchange//DAY-1)*DAY
        for s in SYMBOLS:
            first=db.execute('SELECT MIN(open_ms) FROM candles WHERE symbol=?',(s,)).fetchone()[0]
            cursor=first if first is not None else last_closed-89*DAY;pages=0
            while cursor<=last_closed:
                pages+=1
                if pages>20:raise ValueError('source_history_incomplete')
                raw=fetch('/api/v3/klines',{'symbol':s,'interval':'1d','startTime':cursor,'endTime':last_closed+DAY-1,'limit':1000})
                days=parse_days(raw,exchange,minimum=1);received=clock()
                if days[0]['open_ms']!=cursor or days[-1]['open_ms']>last_closed:raise ValueError('source_history_incomplete')
                db.execute('BEGIN IMMEDIATE');ingest(db,s,days,received);setmeta(db,'last_fetch_ms',received);db.commit()
                cursor=days[-1]['open_ms']+DAY
        if meta(db,'halted'):return
        for s in SYMBOLS:baseline(db,s,version)
        for s in SYMBOLS:
            q=public_quote(s);db.execute('BEGIN IMMEDIATE');record_mark(db,s,q);db.commit()
        boundary=meta(db,'next_boundary');current=clock()
        if current<boundary:return
        if current>boundary+WINDOW:
            db.execute('BEGIN IMMEDIATE');setmeta(db,'halted',True);setmeta(db,'error','missed_observation_window');event(db,'missed:'+str(boundary),current,{'boundary_ms':boundary});db.commit();return
        db.execute('BEGIN IMMEDIATE')
        for s in SYMBOLS:
            rows=db.execute('SELECT * FROM candles WHERE symbol=? AND open_ms<=? ORDER BY open_ms DESC LIMIT 61',(s,boundary-DAY)).fetchall();rows=list(reversed(rows))
            if len(rows)!=61 or rows[-1]['open_ms']!=boundary-DAY or any(rows[i]['open_ms']+DAY!=rows[i+1]['open_ms'] for i in range(60)):raise ValueError('warmup_gap')
            closes=[json.loads(x['payload'])['close'] for x in rows]
            inputs_hash=hashlib.sha256('|'.join(x['hash'] for x in rows).encode()).hexdigest()
            for scenario in ('base','stress'):
                a=state(db,s,scenario,'strategy');action,level=decision(closes,D(a['qty'])>0,a['level']);ts=clock()
                if action=='HOLD' and D(a['qty'])>0:a['level']=level;save(db,s,scenario,'strategy',a)
                db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?,NULL,?,?,?)',(boundary,s,scenario,action,level,ts,rows[-1]['hash'],inputs_hash,version))
        db.commit()
        commit_observed=clock();db.execute('BEGIN IMMEDIATE');db.execute('UPDATE decisions SET commit_seen_ms=? WHERE boundary_ms=? AND commit_seen_ms IS NULL',(commit_observed,boundary));db.commit()
        for s in SYMBOLS:apply_decisions(db,s,boundary)
        db.execute('BEGIN IMMEDIATE');setmeta(db,'next_boundary',boundary+DAY);setmeta(db,'error',None);db.commit()
    except Exception as e:
        if db.in_transaction:db.rollback()
        db.execute('BEGIN IMMEDIATE');setmeta(db,'error',str(e) if isinstance(e,ValueError) and str(e) in ('clock_skew','bad_candle','incomplete_or_discontinuous_candles','source_history_incomplete','warmup_gap','execution_window_expired','quote_freshness_unconfirmed','code_version_changed','position_missing','quote_before_start') else 'public_or_internal_failure')
        if isinstance(e,ValueError) and str(e) in ('source_history_incomplete','incomplete_or_discontinuous_candles','bad_candle'):setmeta(db,'halted',True)
        if db.execute("SELECT 1 FROM quotes WHERE status='claimed'").fetchone():setmeta(db,'halted',True)
        boundary=meta(db,'next_boundary')
        if boundary is not None and clock()>boundary+WINDOW:
            setmeta(db,'halted',True);setmeta(db,'error','missed_observation_window')
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?)',('missed:'+str(boundary),clock(),canonical({'boundary_ms':boundary})))
        db.commit()
    finally:public_quote=old_quote

def status(db):
    current=now();accounts=[]
    for row in db.execute('SELECT * FROM accounts ORDER BY symbol,scenario,kind'):
        a=json.loads(row['state']);q=meta(db,'quote:'+row['symbol']);fresh=q is not None and 0<=current-q['observed_ms']<=3700000
        slip=D('.0005') if row['scenario']=='base' else D('.001')
        equity=D(a['cash'])+D(a['qty'])*D(q['price'])*(1-slip)*D('.999') if fresh else (D(a['cash']) if not D(a['qty']) else None)
        accounts.append({'symbol':row['symbol'],'scenario':row['scenario'],'kind':row['kind'],'cash':a['cash'],'qty':a['qty'],'cost':a['cost'],'exit_level':a['level'],'realized':a['realized'],'equity':str(equity) if equity is not None else None,'net':str(equity-D(1000)) if equity is not None else None,'max_drawdown':a['max_drawdown'],'mark_utc':utc(q['observed_ms']) if q else None})
    latest={s:db.execute('SELECT MAX(open_ms) FROM candles WHERE symbol=?',(s,)).fetchone()[0] for s in SYMBOLS}
    return {'start_utc':utc(meta(db,'start_ms')),'strategy_sha256':meta(db,'version'),'next_boundary_utc':utc(meta(db,'next_boundary')),'last_received_utc':utc(meta(db,'last_fetch_ms')) if meta(db,'last_fetch_ms') else None,'halted':meta(db,'halted'),'error':meta(db,'error'),'missed_windows':db.execute("SELECT COUNT(*) FROM events WHERE id LIKE 'missed:%'").fetchone()[0],'latest_candles':{s:utc(t) if t is not None else None for s,t in latest.items()},'decisions':db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],'accounts':accounts,'as_of_utc':utc(current)}

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=('start','once','daemon','status','backup'));p.add_argument('--state',required=True);a=p.parse_args();path=Path(a.state);path.parent.mkdir(parents=True,exist_ok=True)
    if a.command in ('status','backup'):
        if not path.exists():raise ValueError('state_missing')
        db=connect(path)
        if a.command=='status':print(json.dumps(status(db),ensure_ascii=False))
        else:backup(db,str(path)+'.backup');print('backup_integrity_ok')
        db.close();return
    with open(str(path)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        version=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if a.command=='start':
            init(path,version);a.command='once'
        db=connect(path)
        if version!=meta(db,'version'):raise ValueError('code_version_changed')
        while True:
            cycle(db,version);backup(db,str(path)+'.backup');print(json.dumps(status(db),ensure_ascii=False),flush=True)
            if a.command=='once':break
            remain=(meta(db,'next_boundary')-now())/1000
            wait=(max(2,min(3600,remain+2)) if remain>0 else 60) if not meta(db,'halted') else 3600
            time.sleep(wait)
if __name__=='__main__':main()
