"""Existing-server adapter: free local analysis; paid path is opt-in/sole-budget."""
import argparse,json,os,tempfile,time,fcntl
from pathlib import Path
from research.pattern_ai_capture import capture
from research.pattern_ai_snapshot import build

PERIOD_MS=4*60*60*1000

def paid_boundary_allowed(now_ms,boundary_ms):
    """A single explicit boundary limits paid work; the regular gate still owns retries."""
    if type(now_ms) is not int or type(boundary_ms) is not int or boundary_ms%PERIOD_MS:
        return False
    return now_ms//PERIOD_MS*PERIOD_MS==boundary_ms


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


def main():
    p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--derivatives-context',action='store_true');p.add_argument('--technical-output',required=True)
    p.add_argument('--paid-existing-budget',action='store_true');p.add_argument('--paid-boundary-ms',type=int);p.add_argument('--state');p.add_argument('--gpt-output');a=p.parse_args()
    if a.paid_existing_budget != (a.paid_boundary_ms is not None) or (a.paid_boundary_ms is not None and a.paid_boundary_ms%PERIOD_MS):p.error('paid mode requires exactly one explicit four-hour boundary')
    bundle=json.loads(Path(a.model).read_text());cached=[]
    def fetch():
        if cached:return cached[0]
        raw=capture('BTCUSDT',derivatives=a.derivatives_context);snapshot,base=build(raw,bundle,int(time.time()*1000));publish(snapshot['technical_context'],a.technical_output)
        cached.append((snapshot,base));return cached[0]
    now=int(time.time()*1000);target=Path(a.technical_output)
    if not target.parent.is_dir():raise ValueError('existing_public_directory_required')
    lockfd=os.open(target.parent/'.technical-analysis.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if now%900000<60000:
            local={'status':'waiting_for_mature_closed_bar'}
        else:
            old=None
            if target.is_file() and not target.is_symlink() and target.stat().st_size<=30000:
                try:old=json.loads(target.read_text())
                except (ValueError,OSError):pass
            if old and old.get('signal_ms')==now//900000*900000 and old.get('ml',{}).get('model_id')==bundle['model_id'] and old.get('as_of_ms',now+1)<=now and (not a.derivatives_context or old.get('derivatives',{}).get('version')=='derivatives-context-v1'):
                local={'status':'already_published','gpt_called':False}
            else:
                snapshot,_=fetch();local={'status':'local_analysis_published','packet_id':snapshot['technical_context']['packet_id'],'gpt_called':False,'orders_enabled':False}
        if a.paid_existing_budget and paid_boundary_allowed(now,a.paid_boundary_ms):
            if not a.state or not a.gpt_output:p.error('existing --state and --gpt-output required')
            from research.adaptive_regular import tick
            # No alternate ledger: existing owner/schema/auth/window/cap/locks.
            result={'local':local,'gpt':tick(a.state,a.gpt_output,fetch=fetch)}
        else:result=local
    finally:
        fcntl.flock(lockfd,fcntl.LOCK_UN);os.close(lockfd)
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
