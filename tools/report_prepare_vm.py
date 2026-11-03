"""Prepare report-only update and backup without stopping live paper services."""
import hashlib,importlib.util,json,os,re,shutil,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');OLD=Path('/opt/trading-bot-margin-8a692e3')
INSTALLED='8a692e35032bafc33895de4812eb77c59781878c';ARCHIVE=HOME/'report-updates/20261103a'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
UNITS=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-monitor.service')
ALLOWED={'trading_bot/status_summary.py','trading_bot/margin_status_reports.py','trading_bot/paper_wallet_live_report.py',
         'trading_bot/cross_worker.py','trading_bot/unified_worker.py','trading_bot/margin_worker.py','trading_bot/report_update_guard.py'}

def call(*args,timeout=30):return subprocess.check_output(args,text=True,timeout=timeout).strip()

def main():
    if os.geteuid()!=0 or call('hostname')!='tradingbot-paper-test' or len(sys.argv)!=2 or not re.fullmatch('[0-9a-f]{40}',sys.argv[1]):raise SystemExit('Unexpected host or candidate')
    candidate=sys.argv[1];target=Path('/opt/trading-bot-reports-'+candidate[:7])
    if target.exists() or ARCHIVE.exists() or target.is_symlink() or ARCHIVE.is_symlink():raise ValueError('Existing preparation requires inspection')
    if call('git','-C',str(OLD),'rev-parse','HEAD')!=INSTALLED or call('git','-C',str(OLD),'status','--porcelain','--untracked-files=no'):raise ValueError('Installed checkout changed')
    sys.path.insert(0,str(OLD))
    from trading_bot.paper_coordinated_backup import coordinated_backup
    verifier_path=HOME/'margin-cutovers/20261103b/margin_verify_vm.py'
    spec=importlib.util.spec_from_file_location('installed_margin_verifier',verifier_path);verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
    deadline=time.monotonic()+45
    while True:
        try:health=verifier._market_health(OLD,held=False);break
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError):
            if time.monotonic()>=deadline:raise ValueError('Existing paper services not healthy')
            time.sleep(2)
    if health['paused']['cross'] is not True:raise ValueError('Cross is not paused')
    marker=HOME/'cross-cutovers/service-start-interlock'
    if marker.exists() or marker.is_symlink():raise ValueError('Existing service interlock requires inspection')
    with tempfile.TemporaryDirectory(prefix='.reports-candidate-',dir='/opt') as tmp:
        repo=Path(tmp)/'repo'
        subprocess.run(['git','clone','--quiet','--branch','research/margin-report-clarity','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=90)
        subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',candidate],check=True,timeout=30)
        if call('git','-C',str(repo),'rev-parse','HEAD')!=candidate or call('git','-C',str(repo),'status','--porcelain','--untracked-files=all'):raise ValueError('Candidate checkout mismatch')
        changed=set(call('git','-C',str(repo),'diff','--name-only',INSTALLED,candidate,'--','trading_bot').splitlines())
        if changed!=ALLOWED:raise ValueError('Candidate changes exceed reviewed report scope')
        os.chmod(repo,0o755);os.rename(repo,target)
    ARCHIVE.mkdir(mode=0o700,parents=True);staged=ARCHIVE/'candidate-units';staged.mkdir(mode=0o700)
    for unit in UNITS:
        source=Path('/etc/systemd/system')/unit
        if source.is_symlink() or not source.is_file():raise ValueError('Invalid existing unit')
        data=source.read_bytes();old=str(OLD).encode();new=str(target).encode()
        if data.count(old)!=1 or b'WorkingDirectory='+old not in data:raise ValueError('Unexpected unit working directory')
        (staged/unit).write_bytes(data.replace(old,new));os.chmod(staged/unit,0o600)
    coordinated_backup(FINANCIAL,QUEUE,OBSERVER,ARCHIVE/'before',timeout=15)
    entries=json.loads((ARCHIVE/'before/manifest.json').read_text())['files']
    if {e['name'] for e in entries}!={'financial.db','queue.db','observation.db'}:raise ValueError('Incomplete backup')
    with tempfile.TemporaryDirectory(prefix='restore-check-',dir=ARCHIVE) as tmp:
        for entry in entries:
            dest=Path(tmp)/entry['name'];shutil.copyfile(ARCHIVE/'before'/entry['name'],dest)
            with dest.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=entry['sha256']:raise ValueError('Backup hash mismatch')
            with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Backup restore check failed')
    meta={'candidate':candidate,'installed_before':INSTALLED,'target':str(target),'prepared_at':time.time(),'paused':health['paused']}
    (ARCHIVE/'prepared.json').write_text(json.dumps(meta,sort_keys=True));os.chmod(ARCHIVE/'prepared.json',0o600)
    print(json.dumps({'candidate':candidate,'archive':str(ARCHIVE),'backup_restored_and_verified':True,'paused':health['paused'],'services_changed':False},sort_keys=True),flush=True)

if __name__=='__main__':main()
