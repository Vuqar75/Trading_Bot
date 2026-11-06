"""Guarded, one-time report-only update of the authorised paper services.

The financial and command databases are never restored or replaced. A failure
after any candidate service starts leaves the deployment hold in place and
keeps all recoverable protection services running for manual inspection.
"""
import hashlib,json,os,shutil,signal,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');ARCHIVE=HOME/'adaptiveai-updates/20261106c'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
UNITDIR=Path('/etc/systemd/system');MARKER=HOME/'cross-cutovers/service-start-interlock'
SERVICES=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-monitor.service')
WATCH='tradingbot-watchdog.service'; WATCH_TIMER='tradingbot-watchdog.timer'
AUX_TIMERS=(WATCH_TIMER,'tradingbot-external-hourly.timer','tradingbot-external-daily.timer','tradingbot-heartbeat.timer')
AUX_SERVICES=(WATCH,'tradingbot-external-backup@hourly.service','tradingbot-external-backup@daily.service','tradingbot-heartbeat.service')
ALL_UNITS=SERVICES+('tradingbot-paper-backup.service',WATCH,'tradingbot-external-backup@.service','tradingbot-heartbeat.service')
OLD=SERVICES;TIMER='tradingbot-paper-backup.timer';BACKUP='tradingbot-paper-backup.service'
FREEZE='adaptiveai:freeze:20261106c';INTERLOCK='cross:freeze:adaptiveai-20261106c'

def command(*args,timeout=180):
    return subprocess.run(['systemctl',*args],capture_output=True,text=True,timeout=timeout,check=True).stdout.strip()

def active(unit):
    r=subprocess.run(['systemctl','show',unit,'--property=ActiveState','--value'],capture_output=True,text=True,timeout=20)
    return r.stdout.strip() if not r.returncode else None

def property_value(unit,key):
    r=subprocess.run(['systemctl','show',unit,'--property='+key,'--value'],capture_output=True,text=True,timeout=20)
    return r.stdout.strip() if not r.returncode else None

def process_counts():
    names={'spot':b'trading_bot.app','futures':b'trading_bot.futures_app','cross':b'trading_bot.cross_app',
           'margin':b'trading_bot.margin_app','monitor':b'trading_bot.paper_monitor'}
    counts={name:0 for name in names}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:args=(proc/'cmdline').read_bytes().split(b'\0')
        except OSError:continue
        for name,arg in names.items():counts[name]+=int(arg in args)
    return counts

def main(*,fault=lambda stage:None):
    if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected host')
    if not ARCHIVE.is_dir() or ARCHIVE.is_symlink():raise ValueError('Prepared deployment missing')
    if (ARCHIVE/'switch-started').exists():raise ValueError('Switch already attempted; inspect archive')
    prepared=json.loads((ARCHIVE/'prepared.json').read_text());CANDIDATE=prepared['candidate'];TARGET=Path(prepared['target'])
    if not TARGET.is_dir() or TARGET.is_symlink():raise ValueError('Candidate directory missing')
    if subprocess.check_output(['git','-C',str(TARGET),'rev-parse','HEAD'],text=True).strip()!=CANDIDATE:
        raise ValueError('Candidate mismatch')
    if subprocess.check_output(['git','-C',str(TARGET),'status','--porcelain','--untracked-files=all'],text=True).strip():raise ValueError('Candidate checkout changed')
    sys.path.insert(0,str(TARGET))
    from trading_bot.cross_service_interlock import arm_interlock,release_interlock
    from trading_bot.cross_unit_switch import hold_writer_locks,switch_unit_files,restore_unit_files,_write
    from trading_bot.margin_rollback_guard import may_restore_pre_margin_code
    from trading_bot.report_update_guard import freeze_report_update
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.unified_queue import UnifiedQueue
    stages=ARCHIVE/'candidate-units';unit_archive=ARCHIVE/'unit-archive'
    if unit_archive.exists() or unit_archive.is_symlink():raise ValueError('Existing unit archive requires inspection')
    if any((UNITDIR/name).is_symlink() or not (UNITDIR/name).is_file() for name in ALL_UNITS):raise ValueError('Invalid existing unit')
    original={name:(UNITDIR/name).read_bytes() for name in ALL_UNITS}
    if MARKER.exists():raise ValueError('Existing interlock')
    for name in OLD:
        if active(name)!='active':raise ValueError('Existing service not active')
    if active(TIMER)!='active' or any(active(u)!='active' for u in AUX_TIMERS):raise ValueError('Backup, watchdog or heartbeat timer inactive')
    if process_counts()!={'spot':1,'futures':1,'cross':1,'margin':1,'monitor':1}:raise ValueError('Unexpected paper process set')
    plan=json.loads((stages/'manifest.json').read_text())
    if set(plan)!=set(ALL_UNITS):raise ValueError('Unit plan mismatch')
    for name in ALL_UNITS:
        staged=(stages/name).read_bytes()
        if (hashlib.sha256(original[name]).hexdigest()!=plan[name]['source_sha256'] or
            hashlib.sha256(staged).hexdigest()!=plan[name]['staged_sha256']):
            raise ValueError('Existing or staged unit changed after preparation')
    unit_archive.mkdir(mode=0o700)
    _write(ARCHIVE/'switch-started',json.dumps({'at':time.time(),'candidate':CANDIDATE}).encode(),0o600)
    def mark(stage):
        _write(ARCHIVE/'switch-state',json.dumps({'stage':stage,'at':time.time()}).encode(),0o600)
        print('ADAPTIVEAI_SWITCH',stage,flush=True)
        fault(stage)
    def paused():
        with closing(sqlite3.connect(FINANCIAL.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
            hold=db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()
            spot=db.execute("SELECT value FROM runtime_meta WHERE key='paused'").fetchone()
            future=db.execute("SELECT value FROM futures_meta WHERE key='paused'").fetchone()
            cross=db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
            margin=db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()
            return bool(hold and hold[0]=='true' and spot and json.loads(spot[0]) is True and future and json.loads(future[0]) is True and cross and cross[0]==1 and margin and margin[0]==1)
    def stopped():
        return (all(active(unit)=='inactive' for unit in (*OLD,TIMER,BACKUP,*AUX_TIMERS,*AUX_SERVICES)) and
                process_counts()=={'spot':0,'futures':0,'cross':0,'margin':0,'monitor':0} and paused())
    def unit_match():
        return all((UNITDIR/name).is_file() and not (UNITDIR/name).is_symlink() and (UNITDIR/name).read_bytes()==(stages/name).read_bytes() for name in ALL_UNITS)
    def units_loaded():
        for name in SERVICES:
            expected=str(UNITDIR/(name+'.d')/'90-paper-cross-interlock.conf')
            paths=property_value(name,'DropInPaths')
            if (property_value(name,'FragmentPath')!=str(UNITDIR/name) or property_value(name,'NeedDaemonReload')!='no' or
                paths is None or expected not in paths.split()):return False
        return True
    def restored():return all((UNITDIR/name).read_bytes()==original[name] for name in ALL_UNITS)
    def reload_units():
        try:command('daemon-reload');return True
        except (subprocess.SubprocessError,OSError):return False
    def terminate(signum,frame):raise RuntimeError('Interrupted by signal')
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    armed=False;stopping=False;starting=False;aux_stopped=False
    try:
        aux_stopped=True;command('stop',*AUX_TIMERS,*AUX_SERVICES)
        shared=SharedPaperStore(FINANCIAL);queue=UnifiedQueue(QUEUE)
        try:
            deadline=time.monotonic()+45
            while True:
                try:frozen=freeze_report_update(shared,queue,FREEZE,namespace='adaptiveai');break
                except ValueError as exc:
                    if not any(x in str(exc) for x in ('Incomplete cross data','Stale cross mark','Pending commands require processing','Isolated funding requires reconciliation')) or time.monotonic()>=deadline:raise
                    time.sleep(2)
        finally:queue.close();shared.close()
        mark('entries_frozen')
        arm_interlock(UNITDIR,MARKER,SERVICES,INTERLOCK,reload_units=reload_units);armed=True
        mark('start_interlock_armed')
        stopping=True;command('stop',*OLD,TIMER,BACKUP,timeout=240)
        if not stopped():raise ValueError('Paper services did not stop safely')
        mark('services_stopped')
        with hold_writer_locks(FINANCIAL):
            if not stopped():raise ValueError('Stopped state changed')
            coordinated_backup(FINANCIAL,QUEUE,OBSERVER,ARCHIVE/'stopped',timeout=15)
            entries=json.loads((ARCHIVE/'stopped/manifest.json').read_text())['files']
            if {e['name'] for e in entries}!={'financial.db','queue.db','observation.db'}:raise ValueError('Stopped backup incomplete')
            with tempfile.TemporaryDirectory(prefix='restore-check-',dir=ARCHIVE) as tmp:
                for e in entries:
                    dest=Path(tmp)/e['name'];shutil.copyfile(ARCHIVE/'stopped'/e['name'],dest)
                    with dest.open('rb') as f:
                        if hashlib.file_digest(f,'sha256').hexdigest()!=e['sha256']:raise ValueError('Backup hash mismatch')
                    with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as db:
                        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Backup restore check failed')
            mark('stopped_backup_verified')
            switch_unit_files(UNITDIR,stages,unit_archive,ALL_UNITS,assert_safe=stopped)
            command('daemon-reload')
            if not unit_match() or not units_loaded():raise ValueError('Candidate units or interlocks do not match staged files')
            release_interlock(UNITDIR,MARKER,SERVICES,INTERLOCK,verify_safe=lambda:stopped() and unit_match() and units_loaded())
            armed=False;mark('candidate_units_switched')
        starting=True
        for unit in ('tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-spot.service','tradingbot-monitor.service',TIMER,*AUX_TIMERS):
            command('start',unit)
        mark('candidate_services_started_on_hold')
    except BaseException as exc:
        print('ADAPTIVEAI_SWITCH_ERROR',type(exc).__name__,str(exc),flush=True)
        if stopping and not starting:
            try:
                if not (unit_archive/'unit-switch.json').exists():
                    # No unit was changed; protection can be restarted even if
                    # stopping failed part-way and some old services are alive.
                    if armed:
                        release_interlock(UNITDIR,MARKER,SERVICES,INTERLOCK,verify_safe=lambda:restored() and paused())
                        armed=False
                else:
                    with hold_writer_locks(FINANCIAL):
                        def safe_rollback():
                            if not (ARCHIVE/'stopped/financial.db').is_file():return False
                            return may_restore_pre_margin_code(ARCHIVE/'stopped/financial.db',FINANCIAL,ARCHIVE/'stopped/queue.db',QUEUE)['safe_for_old_code']
                        restore_unit_files(UNITDIR,unit_archive,ALL_UNITS,assert_safe=stopped,verify_rollback=safe_rollback)
                        command('daemon-reload')
                        if armed:
                            release_interlock(UNITDIR,MARKER,SERVICES,INTERLOCK,verify_safe=lambda:stopped() and restored())
                            armed=False
                for unit in ('tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-spot.service','tradingbot-monitor.service',TIMER,*AUX_TIMERS):command('start',unit)
                mark('old_services_restored_on_hold')
            except Exception as rollback_error:
                print('ADAPTIVEAI_ROLLBACK_REFUSED',type(rollback_error).__name__,str(rollback_error),flush=True)
        elif starting:
            # Never restore a snapshot or old units after a new writer may have
            # committed a financial event. Preserve the hold and start any
            # remaining protective workers where possible.
            for unit in ('tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-spot.service','tradingbot-monitor.service',TIMER,*AUX_TIMERS):
                try:command('start',unit)
                except Exception:pass
            mark('candidate_requires_inspection_on_hold')
        elif aux_stopped:
            for unit in AUX_TIMERS:
                try:command('start',unit)
                except Exception:pass
        raise

if __name__=='__main__':main()
