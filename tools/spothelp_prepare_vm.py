"""Prepare report-only update and backup without stopping live paper services."""
import hashlib,importlib.util,json,os,re,shutil,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');OLD=Path('/opt/trading-bot-finalaudit-29d1fc6')
INSTALLED='29d1fc6061902b00c49739b4b74c2f6285a15a3d';ARCHIVE=HOME/'spothelp-updates/20261104a'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
UNITS=('tradingbot-spot.service',)
ALLOWED={'trading_bot/telegram_help.py','trading_bot/unified_queue.py','trading_bot/report_update_guard.py','trading_bot/spot_only_update_lock.py','trading_bot/spot_only_interlock.py'}

def call(*args,timeout=30):return subprocess.check_output(args,text=True,timeout=timeout).strip()

def main():
    if os.geteuid()!=0 or call('hostname')!='tradingbot-paper-test' or len(sys.argv)!=2 or not re.fullmatch('[0-9a-f]{40}',sys.argv[1]):raise SystemExit('Unexpected host or candidate')
    candidate=sys.argv[1];target=Path('/opt/trading-bot-spothelp-'+candidate[:7])
    if target.exists() or ARCHIVE.exists() or target.is_symlink() or ARCHIVE.is_symlink():raise ValueError('Existing preparation requires inspection')
    if call('git','-C',str(OLD),'rev-parse','HEAD')!=INSTALLED or call('git','-C',str(OLD),'status','--porcelain','--untracked-files=no'):raise ValueError('Installed checkout changed')
    sys.path.insert(0,str(OLD))
    from trading_bot.paper_coordinated_backup import coordinated_backup
    verifier_path=HOME/'margin-cutovers/20261103b/margin_verify_vm.py'
    spec=importlib.util.spec_from_file_location('installed_margin_verifier',verifier_path);verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier);verifier.ARCHIVE=HOME/'boundary-updates/20261104a'
    deadline=time.monotonic()+45
    while True:
        try:health=verifier._market_health(OLD,held=False);break
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError):
            if time.monotonic()>=deadline:raise ValueError('Existing paper services not healthy')
            time.sleep(2)
    if any(health['paused'][market] is not True for market in ('spot','futures','cross','margin')):raise ValueError('Isolated futures, cross and margin must remain paused')
    marker=HOME/'cross-cutovers/spothelp-start-interlock'
    if marker.exists() or marker.is_symlink():raise ValueError('Existing service interlock requires inspection')
    with tempfile.TemporaryDirectory(prefix='.reports-candidate-',dir='/opt') as tmp:
        repo=Path(tmp)/'repo'
        subprocess.run(['git','clone','--quiet','--branch','research/telegram-help-guide','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=90)
        subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',candidate],check=True,timeout=30)
        if call('git','-C',str(repo),'rev-parse','HEAD')!=candidate or call('git','-C',str(repo),'status','--porcelain','--untracked-files=all'):raise ValueError('Candidate checkout mismatch')
        changed=set(call('git','-C',str(repo),'diff','--name-only',INSTALLED,candidate,'--','trading_bot').splitlines())
        if changed!=ALLOWED:raise ValueError('Candidate changes exceed reviewed report scope')
        if call('git','-C',str(repo),'diff','--name-only',INSTALLED,candidate,'--','deploy'):raise ValueError('Unexpected deployment configuration changes')
        os.chmod(repo,0o755);os.rename(repo,target)
    subprocess.run(['git','config','--system','--add','safe.directory',str(target)],check=True,timeout=20)
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
    # A persistent, enabled on-boot recovery service protects interrupted
    # switches. The coordinator also binds it as OnFailure before stopping.
    recovery=Path('/etc/systemd/system/tradingbot-spothelp-recovery.service')
    if recovery.exists() or recovery.is_symlink():raise ValueError('Existing recovery unit requires inspection')
    content=('[Unit]\nDescription=Recover paper protection after an interrupted spothelp update\nAfter=local-fs.target\n'
             '[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -B '+str(target)+'/tools/spothelp_recovery_vm.py\n'
             '[Install]\nWantedBy=multi-user.target\n')
    from trading_bot.cross_unit_switch import _write
    _write(recovery,content.encode(),0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
    subprocess.run(['systemctl','enable','tradingbot-spothelp-recovery.service'],check=True,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print(json.dumps({'candidate':candidate,'archive':str(ARCHIVE),'backup_restored_and_verified':True,'paused':health['paused'],'services_changed':False},sort_keys=True),flush=True)

if __name__=='__main__':main()
