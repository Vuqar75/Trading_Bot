"""Weekly isolated external-backup verification, with no observer startup."""
import argparse,hashlib,io,json,os,sqlite3,tarfile,tempfile,time,urllib.parse,urllib.request
from decimal import Decimal,InvalidOperation
from pathlib import Path
from research import donchian_external_backup as b,donchian_monitor as m

def check_sqlite(raw,manifest,work):
    with tempfile.TemporaryDirectory(prefix='restore-audit-',dir=work) as folder:
        path=Path(folder)/'observer.sqlite';path.write_bytes(raw)
        db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('sqlite_integrity')
            if m.meta(db,'start_ms')!=m.START or manifest['start_ms']!=m.START:raise ValueError('original_start_mismatch')
            if m.meta(db,'version')!=manifest['strategy_sha256'] or m.meta(db,'quote_protocol')!=manifest['quote_protocol']:raise ValueError('protocol_mismatch')
            transition=m.unpack(db.execute("SELECT payload FROM events WHERE id='infra:quote-protocol-v2'").fetchone())
            if not transition or transition.get('adapter_sha256')!=manifest['adapter_sha256']:raise ValueError('adapter_mismatch')
            expected={(s,sc,k) for s in m.SYMBOLS for sc in m.SCENARIOS for k in m.KINDS};actual=set();accounts={}
            for row in db.execute('SELECT * FROM accounts'):
                actual.add((row['symbol'],row['scenario'],row['kind']));a=json.loads(row['state'])
                for key in ('cash','qty','cost','realized','peak','max_drawdown'):
                    value=Decimal(a[key])
                    if not value.is_finite() or (key!='realized' and value<0):raise ValueError('account_value_invalid')
                if Decimal(a['qty'])==0 and Decimal(a['cost'])!=0:raise ValueError('account_cost_mismatch')
                accounts[(row['symbol'],row['scenario'],row['kind'])]=a
            if actual!=expected:raise ValueError('account_set_mismatch')
            for row in db.execute('SELECT symbol,open_ms,hash,payload FROM candles'):
                if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['hash']:raise ValueError('candle_hash_mismatch')
                if not db.execute('SELECT 1 FROM candle_versions WHERE symbol=? AND open_ms=? AND hash=?',(row['symbol'],row['open_ms'],row['hash'])).fetchone():raise ValueError('candle_version_missing')
            shadow={key:{'cash':Decimal(1000),'qty':Decimal(0),'cost':Decimal(0),'realized':Decimal(0)} for key in expected}
            for row in db.execute('SELECT id,at_ms,payload FROM events ORDER BY at_ms,id'):
                event=json.loads(row['payload'])
                if not row['id'] or not isinstance(row['at_ms'],int) or not isinstance(event,dict):raise ValueError('event_invalid')
                if event.get('kind') not in ('strategy','buy_and_hold') or event.get('result')!='filled':continue
                parts=row['id'].split(':');kind='hold' if event['kind']=='buy_and_hold' else 'strategy'
                if len(parts)<3 or (parts[-2],parts[-1],kind) not in shadow:raise ValueError('event_account_invalid')
                a=shadow[(parts[-2],parts[-1],kind)];qty=Decimal(event['quantity']);price=Decimal(event['fill_price']);fee=Decimal(event['fee'])
                if any(not v.is_finite() for v in (qty,price,fee)) or qty<=0 or price<=0 or fee<0:raise ValueError('event_amount_invalid')
                value=qty*price
                if event.get('side')=='BUY':
                    if a['qty']!=0:raise ValueError('event_duplicate_entry')
                    a['cash']-=value+fee;a['qty']=qty;a['cost']=value+fee
                elif event.get('side')=='SELL':
                    if a['qty']!=qty:raise ValueError('event_quantity_mismatch')
                    a['cash']+=value-fee;a['realized']+=value-fee-a['cost'];a['qty']=a['cost']=Decimal(0)
                else:raise ValueError('event_side_invalid')
            for key,expected_account in shadow.items():
                if any(Decimal(accounts[key][field])!=expected_account[field] for field in ('cash','qty','cost','realized')):raise ValueError('account_event_reconciliation')
            for d in db.execute('SELECT * FROM decisions'):
                if d['action'] not in ('BUY','SELL','HOLD') or d['symbol'] not in m.SYMBOLS or d['scenario'] not in m.SCENARIOS:raise ValueError('decision_invalid')
                if d['action'] in ('BUY','SELL'):
                    ident=f"{d['boundary_ms']}:{d['symbol']}:{d['scenario']}";ev=m.unpack(db.execute('SELECT payload FROM events WHERE id=?',(ident,)).fetchone())
                    claim=db.execute('SELECT status FROM quotes WHERE id=?',(f"{d['boundary_ms']}:{d['symbol']}",)).fetchone()
                    if not ev and not claim:raise ValueError('missing_decision_evidence')
            counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('accounts','events','decisions','candles','candle_versions')}
            return counts
        finally:db.close()

def verify_object(data,marker,work):
    if hashlib.sha256(data).hexdigest()!=marker['sha256']:raise ValueError('external_object_hash')
    manifest=b.verify(data)
    if manifest!=marker['manifest']:raise ValueError('manifest_mismatch')
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:raw=tar.extractfile('observer.sqlite').read()
    return {'counts':check_sqlite(raw,manifest,work),'start_ms':manifest['start_ms'],'captured_utc_ms':manifest['captured_utc_ms']}

def download(marker):
    name=marker['object']
    if not name.startswith(b.PREFIX) or '/rehearsal/' in name or '/migration/' in name or not name.endswith('.tar.gz'):raise ValueError('backup_object_outside_prefix')
    token=b.metadata_token();url='https://storage.googleapis.com/storage/v1/b/'+b.BUCKET+'/o/'+urllib.parse.quote(name,safe='')+'?alt=media'
    with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as r:return r.read(20000001)

def run(marker=b.WORK/'success.json',control=m.CONTROL,clock=lambda:int(time.time()*1000),fetch=download):
    control=Path(control);control.mkdir(parents=True,exist_ok=True);result={'checked_ms':clock(),'state':'failed','reason':'verification_failed'}
    try:
        marker=json.loads(Path(marker).read_text());result['backup_sha256']=marker['sha256']
        detail=verify_object(fetch(marker),marker,control);result.update(state='verified',reason=None,detail=detail)
    except Exception as e:
        result['reason']=str(e) if isinstance(e,ValueError) and str(e) in ('external_object_hash','manifest_mismatch','sqlite_integrity','original_start_mismatch','protocol_mismatch','adapter_mismatch','account_value_invalid','account_cost_mismatch','account_set_mismatch','candle_hash_mismatch','candle_version_missing','event_invalid','decision_invalid','missing_decision_evidence','backup_object_outside_prefix') else 'download_or_verification_failed'
    m.replace(control/'restore.json',result)
    if result['state']!='verified':raise ValueError(result['reason'])
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--marker',default=str(b.WORK/'success.json'));p.add_argument('--control',default=str(m.CONTROL));a=p.parse_args();print(json.dumps(run(a.marker,a.control),sort_keys=True))
