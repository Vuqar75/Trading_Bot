"""Prepare a pair rollout; no trading service is stopped or started here."""
import hashlib,importlib.util,json,os,shutil,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');OLD=Path('/opt/trading-bot-startguide-0968689')
INSTALLED='096868952339bd4d29d822a811fbd678e2ed631c';ARCHIVE=HOME/'pair-updates/20261105a'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'

def call(*args,timeout=90):return subprocess.check_output(args,text=True,timeout=timeout).strip()

def main():
    if os.geteuid()!=0 or call('hostname')!='tradingbot-paper-test' or len(sys.argv)!=2 or len(sys.argv[1])!=40 or any(c not in '0123456789abcdef' for c in sys.argv[1]):raise ValueError('Unexpected host or candidate')
    candidate=sys.argv[1];target=Path('/opt/trading-bot-pair-'+candidate[:7])
    if target.exists() or ARCHIVE.exists() or target.is_symlink() or ARCHIVE.is_symlink():raise ValueError('Existing preparation requires inspection')
    if call('git','-C',str(OLD),'rev-parse','HEAD')!=INSTALLED or call('git','-C',str(OLD),'status','--porcelain','--untracked-files=no'):raise ValueError('Installed checkout changed')
    sys.path.insert(0,str(OLD))
    from trading_bot.paper_coordinated_backup import coordinated_backup
    verifier_path=HOME/'margin-cutovers/20261103b/margin_verify_vm.py'
    spec=importlib.util.spec_from_file_location('installed_margin_verifier',verifier_path);verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier);verifier.ARCHIVE=HOME/'startguide-updates/20261104a'
    deadline=time.monotonic()+45
    while True:
        try:health=verifier._market_health(OLD,held=False);break
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError):
            if time.monotonic()>=deadline:raise ValueError('Existing services not healthy')
            time.sleep(2)
    if any(health['paused'][m] is not True for m in ('spot','futures','cross','margin')):raise ValueError('All markets must already be paused')
    marker=HOME/'cross-cutovers/service-start-interlock'
    if marker.exists() or marker.is_symlink():raise ValueError('Existing interlock requires inspection')
    with tempfile.TemporaryDirectory(prefix='.pair-candidate-',dir='/opt') as tmp:
        repo=Path(tmp)/'repo'
        call('git','clone','--quiet','--branch','research/paper-pair-selection','https://github.com/Vuqar75/Trading_Bot.git',str(repo))
        call('git','-C',str(repo),'checkout','--quiet','--detach',candidate)
        if call('git','-C',str(repo),'rev-parse','HEAD')!=candidate or call('git','-C',str(repo),'status','--porcelain','--untracked-files=all'):raise ValueError('Candidate mismatch')
        os.chmod(repo,0o755);os.rename(repo,target)
    # Import the staging planner from the verified candidate, not the installed checkout.
    planner_path=target/'trading_bot/pair_rollout_plan.py'
    spec=importlib.util.spec_from_file_location('pair_staging_planner',planner_path);planner=importlib.util.module_from_spec(spec);spec.loader.exec_module(planner)
    ARCHIVE.mkdir(mode=0o700,parents=True);sources=ARCHIVE/'source-units';sources.mkdir(mode=0o700)
    roots={}
    for name in planner.MODULES:
        path=Path('/etc/systemd/system')/name
        if path.is_symlink() or not path.is_file():raise ValueError('Invalid installed unit')
        data=path.read_bytes();(sources/name).write_bytes(data);os.chmod(sources/name,0o600)
        lines=data.decode('utf-8').splitlines();work=[x.partition('=')[2] for x in lines if x.startswith('WorkingDirectory=')]
        if len(work)!=1:raise ValueError('Unit working directory unknown')
        roots[name]=work[0]
    staged=ARCHIVE/'candidate-units';planner.stage_units(sources,staged,installed_root=str(OLD),candidate_root=str(target),installed_roots=roots)
    subprocess.run(['systemd-analyze','verify',*[str(staged/name) for name in planner.MODULES]],check=True,capture_output=True,timeout=60)
    coordinated_backup(FINANCIAL,QUEUE,OBSERVER,ARCHIVE/'before',timeout=30)
    entries=json.loads((ARCHIVE/'before/manifest.json').read_text())['files']
    if {e['name'] for e in entries}!={'financial.db','queue.db','observation.db'}:raise ValueError('Incomplete backup')
    with tempfile.TemporaryDirectory(prefix='restore-check-',dir=ARCHIVE) as tmp:
        for entry in entries:
            dest=Path(tmp)/entry['name'];shutil.copyfile(ARCHIVE/'before'/entry['name'],dest)
            with dest.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=entry['sha256']:raise ValueError('Backup hash mismatch')
            with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('pragma integrity_check').fetchone()[0]!='ok' or db.execute('pragma foreign_key_check').fetchone():raise ValueError('Backup integrity failure')
    (ARCHIVE/'prepared.json').write_text(json.dumps({'candidate':candidate,'target':str(target),'installed_before':INSTALLED,'paused':health['paused'],'at':time.time()},sort_keys=True));os.chmod(ARCHIVE/'prepared.json',0o600)
    # Dedicated systemd oneshot ensures that SIGKILL or manager failure triggers
    # the same on-hold recovery used after a reboot. Never replace old units.
    from trading_bot.cross_unit_switch import _write
    system=Path('/etc/systemd/system')
    recovery=system/'tradingbot-pair-recovery.service';switch=system/'tradingbot-pair-switch.service'
    if any(p.exists() or p.is_symlink() for p in (recovery,switch)):raise ValueError('Pair unit already exists')
    _write(recovery,('[Unit]\nDescription=Recover interrupted paper pair rollout\nAfter=local-fs.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -B '+str(target)+'/tools/pair_recovery_vm.py\n[Install]\nWantedBy=multi-user.target\n').encode(),0o644)
    _write(switch,('[Unit]\nDescription=Guarded paper pair rollout\nOnFailure=tradingbot-pair-recovery.service\n[Service]\nType=oneshot\nTimeoutStartSec=900\nExecStart=/usr/bin/python3 -B '+str(target)+'/tools/pair_switch_vm.py\n').encode(),0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
    subprocess.run(['systemctl','enable','tradingbot-pair-recovery.service'],check=True,timeout=30,capture_output=True)
    print(json.dumps({'candidate':candidate,'archive':str(ARCHIVE),'backup_restored':True,'paused':health['paused'],'services_changed':False},sort_keys=True),flush=True)

if __name__=='__main__':main()
