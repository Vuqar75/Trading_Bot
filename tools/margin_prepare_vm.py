"""Prepare a margin paper deployment without stopping or changing services.

Run explicitly as root on the authorised VM. The protected Telegram settings
are neither read nor copied. A later guarded switch is a separate operation.
"""
import hashlib,json,os,shutil,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path

CANDIDATE='8a692e35032bafc33895de4812eb77c59781878c'
INSTALLED='c05238e38c30018a538447f3607de3ec3dd26bb8'
HOME=Path('/var/lib/tradingbot');OLD=Path('/opt/trading-bot-paper-c05238e')
TARGET=Path('/opt/trading-bot-margin-8a692e3');ARCHIVE=HOME/'margin-cutovers/20261103b'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
UNITS=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-monitor.service')

def call(*args,timeout=30):
    return subprocess.check_output(args,text=True,timeout=timeout).strip()

def main():
    if os.geteuid()!=0 or call('hostname')!='tradingbot-paper-test':raise SystemExit('Unexpected host')
    if TARGET.exists() or ARCHIVE.exists() or TARGET.is_symlink() or ARCHIVE.is_symlink():raise ValueError('Existing staging attempt requires inspection')
    if call('git','-C',str(OLD),'rev-parse','HEAD')!=INSTALLED or call('git','-C',str(OLD),'status','--porcelain','--untracked-files=no'):
        raise ValueError('Installed checkout changed')
    sys.path.insert(0,str(OLD))
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.cross_systemd_controller import SystemdPaperController
    controller=SystemdPaperController(FINANCIAL,'/etc/systemd/system',None,timeout=20)
    if controller._counts()!={'spot':1,'futures':1,'cross':1,'monitor':1} or not controller._relay_locked():raise ValueError('Unexpected running process set')
    if any(controller._property(unit,'WorkingDirectory')!=str(OLD) or controller._property(unit,'ActiveState')!='active' or controller._property(unit,'NeedDaemonReload')!='no' for unit in UNITS):
        raise ValueError('Installed unit state changed')
    if controller._property('tradingbot-margin.service','LoadState') not in (None,'not-found') or controller._property('tradingbot-paper-backup.timer','ActiveState')!='active':
        raise ValueError('Unexpected margin service or backup timer')
    marker=HOME/'cross-cutovers/service-start-interlock'
    if marker.exists() or marker.is_symlink():raise ValueError('Existing service interlock requires inspection')
    shared=SharedPaperStore(FINANCIAL)
    try:
        controller.shared=shared
        deadline=time.monotonic()+45
        while not controller._healthy(cross=True,paused=False):
            if time.monotonic()>=deadline:raise ValueError('Existing paper services not healthy')
            time.sleep(2)
        if shared.deployment_hold() or shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]!=1:
            raise ValueError('Existing pause or hold changed')
        positions={m:shared.snapshot(m)['position'] for m in ('spot','futures')}
        cross=[dict(r) for r in shared.db.execute('SELECT * FROM cross_positions ORDER BY symbol')]
        states={m:shared.snapshot(m)['paused'] for m in ('spot','futures')}
        if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal mismatch')
    finally:shared.close()
    with tempfile.TemporaryDirectory(prefix='.margin-candidate-',dir='/opt') as tmp:
        repo=Path(tmp)/'repo'
        subprocess.run(['git','clone','--quiet','--branch','research/spot-margin-cross-paper','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=90)
        subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',CANDIDATE],check=True,timeout=30)
        if call('git','-C',str(repo),'rev-parse','HEAD')!=CANDIDATE or call('git','-C',str(repo),'status','--porcelain','--untracked-files=all'):
            raise ValueError('Candidate mismatch')
        if call('git','-C',str(repo),'diff','--name-only','5f1717d986e3d64e2263585bc08a04d78904f849',CANDIDATE,'--','trading_bot').splitlines()!=['trading_bot/app.py','trading_bot/margin_rollout.py','trading_bot/margin_valuation_verifier.py']:
            raise ValueError('Candidate trading code differs from tested version')
        os.chmod(repo,0o755);os.rename(repo,TARGET)
    ARCHIVE.mkdir(mode=0o700,parents=True)
    staged=ARCHIVE/'candidate-units';staged.mkdir(mode=0o700)
    for unit in UNITS:
        source=Path('/etc/systemd/system')/unit
        if source.is_symlink() or not source.is_file():raise ValueError('Invalid existing unit')
        data=source.read_bytes();old=str(OLD).encode();new=str(TARGET).encode()
        if data.count(old)!=1 or b'WorkingDirectory='+old not in data:raise ValueError('Unexpected unit working directory')
        (staged/unit).write_bytes(data.replace(old,new));os.chmod(staged/unit,0o600)
    data=(TARGET/'deploy/vm-spot-margin-paper.service').read_bytes()
    if data.count(b'/opt/trading-bot-margin')!=1:raise ValueError('Invalid new unit template')
    (staged/'tradingbot-margin.service').write_bytes(data.replace(b'/opt/trading-bot-margin',str(TARGET).encode()))
    os.chmod(staged/'tradingbot-margin.service',0o600)
    coordinated_backup(FINANCIAL,QUEUE,OBSERVER,ARCHIVE/'before',timeout=15)
    entries=json.loads((ARCHIVE/'before/manifest.json').read_text())['files']
    if {x['name'] for x in entries}!={'financial.db','queue.db','observation.db'}:raise ValueError('Incomplete backup')
    with tempfile.TemporaryDirectory(prefix='restore-check-',dir=ARCHIVE) as tmp:
        for entry in entries:
            dest=Path(tmp)/entry['name'];shutil.copyfile(ARCHIVE/'before'/entry['name'],dest)
            with dest.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=entry['sha256']:raise ValueError('Backup hash mismatch')
            with sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Backup restore check failed')
    metadata={'candidate':CANDIDATE,'installed_before':INSTALLED,'prepared_at':time.time(),'positions':positions,'cross_positions':cross,'paused':states}
    (ARCHIVE/'prepared.json').write_text(json.dumps(metadata,sort_keys=True));os.chmod(ARCHIVE/'prepared.json',0o600)
    print(json.dumps({'candidate':CANDIDATE,'archive':str(ARCHIVE),'backup_restored_and_verified':True,
        'spot_paused':states['spot'],'isolated_paused':states['futures'],'cross_paused':True,'cross_positions':len(cross),
        'services_changed':False},sort_keys=True),flush=True)

if __name__=='__main__':main()
