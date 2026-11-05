"""Isolated systemd adapter for the unchanged prospective strategy."""
import argparse,hashlib,json,os,signal,sqlite3,threading,time
from pathlib import Path
from research import donchian_prospective as p

ORIGINAL='4ec16a367bc5a7e7fa5c80f9a1dbb33ec1fdf0283b949cf1dc91c59eb8a217da'
KEY='infra:quote-protocol-v2'

def upgrade(db,adapter_hash,clock=p.now):
    if p.meta(db,'version')!=ORIGINAL:raise ValueError('unexpected_strategy_version')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT payload FROM events WHERE id=?',(KEY,)).fetchone()
        if row:
            previous=json.loads(row[0])
            if previous['adapter_sha256']!=adapter_hash or previous['protocol']!='trade-time-v2':raise ValueError('adapter_version_changed')
        else:
            effective=clock();record={'protocol':'trade-time-v2','effective_ms':effective,'adapter_sha256':adapter_hash,'strategy_sha256':ORIGINAL,'previous':'ticker-price-v1'}
            p.event(db,KEY,effective,record);p.setmeta(db,'quote_protocol','trade-time-v2')
        db.commit()
    except BaseException:
        db.rollback();raise

def required_event_time(db,symbol):
    row=db.execute("SELECT id FROM quotes WHERE status='claimed' ORDER BY claimed_ms LIMIT 1").fetchone()
    if not row:return None
    ident=row['id']
    if ident=='initial:'+symbol:return p.meta(db,'start_ms')
    suffix=':'+symbol
    if not ident.endswith(suffix) or not ident[:-len(suffix)].isdigit():raise ValueError('unexpected_quote_claim')
    boundary=int(ident[:-len(suffix)]);times=db.execute('SELECT commit_seen_ms FROM decisions WHERE boundary_ms=? AND symbol=?',(boundary,symbol)).fetchall()
    if len(times)!=2 or any(x[0] is None for x in times):raise ValueError('missing_signal_commit')
    return max(boundary-1,max(x[0] for x in times))

def timed_quote(db,symbol,adapter_hash,request=p.request,clock=p.now,sleep=time.sleep):
    floor=required_event_time(db,symbol)
    for attempt in range(3):
        start=clock();before=int(request('/api/v3/time')['serverTime']);rows=request('/api/v3/trades',{'symbol':symbol,'limit':1});after=int(request('/api/v3/time')['serverTime']);received=clock()
        if not isinstance(rows,list) or len(rows)!=1:raise ValueError('trade_source_ambiguous')
        row=rows[0];source=row.get('time');price=p.D(row.get('price','0'))
        if type(row.get('id')) is not int or row['id']<0:raise ValueError('trade_source_ambiguous')
        if type(source) is not int or not price.is_finite() or price<=0 or after<before or received-start>5000 or abs(before-start)>5000 or abs(after-received)>5000:raise ValueError('trade_source_ambiguous')
        if source>after or source>received or received-source>5000:raise ValueError('trade_source_stale_or_future')
        if floor is None or source>floor:
            return {'symbol':symbol,'price':str(price),'observed_ms':received,'request_ms':start,'server_before_ms':before,'server_after_ms':after,'source_trade_ms':source,'source_trade_id':row.get('id'),'source':'binance_spot_recent_trade','quote_protocol':'trade-time-v2','adapter_sha256':adapter_hash}
        if attempt<2:sleep(.4)
    raise ValueError('no_trade_after_signal')

def run(path):
    import fcntl
    path=Path(path);stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    adapter_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if hashlib.sha256(Path(p.__file__).read_bytes()).hexdigest()!=ORIGINAL:raise ValueError('strategy_code_changed')
    with open(str(path)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);db=p.connect(path)
        try:
            upgrade(db,adapter_hash)
            while not stop.is_set():
                quote_error=[]
                def quote(symbol):
                    try:return timed_quote(db,symbol,adapter_hash)
                    except ValueError as e:
                        quote_error.append(str(e) if str(e) in ('trade_source_ambiguous','trade_source_stale_or_future','no_trade_after_signal','missing_signal_commit','unexpected_quote_claim') else 'quote_validation_failed')
                        raise ValueError('quote_freshness_unconfirmed') from None
                p.cycle(db,ORIGINAL,quote=quote);p.backup(db,str(path)+'.backup')
                health=p.status(db);health['service_heartbeat_ms']=p.now();health['quote_protocol']='trade-time-v2';health['adapter_sha256']=adapter_hash;health['quote_diagnostic']=quote_error[-1] if quote_error else None
                tmp=path.parent/'health.json.tmp';tmp.write_text(json.dumps(health,sort_keys=True));os.chmod(tmp,0o600);os.replace(tmp,path.parent/'health.json')
                print(json.dumps({'heartbeat_utc':health['as_of_utc'],'last_received_utc':health['last_received_utc'],'halted':health['halted'],'error':health['error'],'next_boundary_utc':health['next_boundary_utc']}),flush=True)
                remain=(p.meta(db,'next_boundary')-p.now())/1000;wait=(max(2,min(3600,remain+2)) if remain>0 else 60) if not p.meta(db,'halted') else 3600
                stop.wait(wait)
        finally:db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--state',required=True);run(parser.parse_args().state)
