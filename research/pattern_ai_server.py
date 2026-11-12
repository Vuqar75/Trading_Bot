"""Existing-server adapter: free local analysis; paid path is opt-in/sole-budget."""
import argparse,json,os,tempfile,time,fcntl
from pathlib import Path
from research.pattern_ai_capture import capture
from research.pattern_ai_snapshot import build
from research.technical_assets import sidecar

PERIOD_MS=4*60*60*1000
HOST_DENIALS={'public_market_unavailable_http'+str(x) for x in (401,403,451)}

def paid_boundary_allowed(now_ms,boundary_ms):
    """A single explicit boundary limits paid work; the regular gate still owns retries."""
    if type(now_ms) is not int or type(boundary_ms) is not int or boundary_ms%PERIOD_MS:
        return False
    return now_ms//PERIOD_MS*PERIOD_MS==boundary_ms


def cached_packet_matches(old,bundle,asset,now,derivatives):
    return (isinstance(old,dict) and old.get('symbol')==asset and old.get('venue')=='binance-usdm'
        and old.get('feature_version')=='technical-patterns-v1' and old.get('signal_ms')==now//900000*900000
        and old.get('expires_ms')==now//900000*900000+900000 and type(old.get('as_of_ms')) is int
        and old['signal_ms']<=old['as_of_ms']<=now and isinstance(old.get('ml'),dict)
        and old['ml'].get('model_id')==bundle['model_id']
        and (not derivatives or (isinstance(old.get('derivatives'),dict) and old['derivatives'].get('version')=='derivatives-context-v1')))


def publish(packet,path):
    path=Path(path)
    if path.is_symlink() or not path.parent.is_dir():raise ValueError('existing_public_directory_required')
    raw=json.dumps(packet,sort_keys=True,ensure_ascii=False,allow_nan=False)+'\n'
    if len(raw.encode())>30000:raise ValueError('public_packet_too_large')
    fd,tmp=tempfile.mkstemp(prefix='.technical-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o644);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def extra_local(asset,args,bundle):
    """Free-only per-asset work; called under existing lock after BTC/paid work."""
    now=int(time.time()*1000);directory=Path(args.technical_output).parent
    target=sidecar(directory,asset)
    if now%900000<60000:return {'status':'waiting_for_mature_closed_bar','gpt_called':False}
    old=None
    if target.is_file() and not target.is_symlink() and target.stat().st_size<=30000:
        try:old=json.loads(target.read_text())
        except (ValueError,OSError):pass
    if cached_packet_matches(old,bundle,asset,now,args.derivatives_context):
        return {'status':'already_published','gpt_called':False}
    raw=capture(asset,derivatives=args.derivatives_context)
    if (raw.get('symbol'),raw.get('venue'))!=(asset,'binance-usdm'):raise ValueError('technical_capture_market')
    snapshot,_=build(raw,bundle,int(time.time()*1000));packet=snapshot['technical_context']
    if packet.get('symbol')!=asset:raise ValueError('technical_packet_market')
    history='unavailable'
    try:
        from research.technical_plan_history import update
        update(sidecar(directory,asset,'technical-history'),packet,raw,int(time.time()*1000));history='updated'
    except (OSError,ValueError,KeyError,TypeError):pass
    publish(packet,target)
    return {'status':'local_analysis_published','packet_id':packet['packet_id'],'gpt_called':False,'orders_enabled':False,'plan_history':history}


def main():
    p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--derivatives-context',action='store_true');p.add_argument('--technical-output',required=True)
    p.add_argument('--paid-existing-budget',action='store_true');p.add_argument('--paid-boundary-ms',type=int);p.add_argument('--state');p.add_argument('--gpt-output')
    p.add_argument('--extra-symbols',nargs='+',choices=('ETHUSDT','SOLUSDT'),default=[]);a=p.parse_args()
    if len(a.extra_symbols)!=len(set(a.extra_symbols)):p.error('duplicate extra symbol')
    if a.paid_existing_budget != (a.paid_boundary_ms is not None) or (a.paid_boundary_ms is not None and a.paid_boundary_ms%PERIOD_MS):p.error('paid mode requires exactly one explicit four-hour boundary')
    bundle=json.loads(Path(a.model).read_text());cached=[];history_status=['not_updated']
    def fetch():
        if cached:return cached[0]
        raw=capture('BTCUSDT',derivatives=a.derivatives_context);snapshot,base=build(raw,bundle,int(time.time()*1000))
        try:
            from research.technical_plan_history import update
            update(Path(a.technical_output).with_name('technical-history.json'),snapshot['technical_context'],raw,int(time.time()*1000))
            history_status[0]='updated'
        except (OSError,ValueError,KeyError,TypeError):history_status[0]='unavailable'
        publish(snapshot['technical_context'],a.technical_output)
        cached.append((snapshot,base));return cached[0]
    now=int(time.time()*1000);target=Path(a.technical_output)
    if not target.parent.is_dir():raise ValueError('existing_public_directory_required')
    lockfd=os.open(target.parent/'.technical-analysis.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        host_denied=False
        if now%900000<60000:
            local={'status':'waiting_for_mature_closed_bar'}
        else:
            old=None
            if target.is_file() and not target.is_symlink() and target.stat().st_size<=30000:
                try:old=json.loads(target.read_text())
                except (ValueError,OSError):pass
            if cached_packet_matches(old,bundle,'BTCUSDT',now,a.derivatives_context):
                local={'status':'already_published','gpt_called':False}
            else:
                try:
                    snapshot,_=fetch();local={'status':'local_analysis_published','packet_id':snapshot['technical_context']['packet_id'],'gpt_called':False,'orders_enabled':False,'plan_history':history_status[0]}
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    if not a.extra_symbols:raise
                    host_denied=str(exc) in HOST_DENIALS
                    local={'status':'local_analysis_unavailable','gpt_called':False,'orders_enabled':False}
        if a.paid_existing_budget and paid_boundary_allowed(now,a.paid_boundary_ms):
            if not a.state or not a.gpt_output:p.error('existing --state and --gpt-output required')
            from research.adaptive_regular import tick
            # No alternate ledger: existing owner/schema/auth/window/cap/locks.
            if local.get('status')=='local_analysis_unavailable':result={'local':local,'gpt':{'status':'not_started','reason':'btc_analysis_unavailable'}}
            else:result={'local':local,'gpt':tick(a.state,a.gpt_output,fetch=fetch)}
        else:result=local
        if a.extra_symbols:
            result['extra_local']={}
            for asset in a.extra_symbols:
                try:
                    if host_denied:result['extra_local'][asset]={'status':'source_access_denied','gpt_called':False,'orders_enabled':False}
                    else:result['extra_local'][asset]=extra_local(asset,a,bundle)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    host_denied=host_denied or str(exc) in HOST_DENIALS
                    result['extra_local'][asset]={'status':'local_analysis_unavailable','gpt_called':False,'orders_enabled':False}
    finally:
        fcntl.flock(lockfd,fcntl.LOCK_UN);os.close(lockfd)
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
