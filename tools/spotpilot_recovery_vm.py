"""Recover protection after a failed or killed startguide switch, on hold only.

Never restores a database. Only finishes a known staged unit switch or starts
unchanged old units; unknown/mixed units with live workers are refused.
"""
import json,os,sqlite3,subprocess,sys
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');ARCHIVE=HOME/'spotpilot-updates/20261107c'
FINANCIAL=HOME/'paper-financial/shared-paper.db';UNITDIR=Path('/etc/systemd/system')
MARKER=HOME/'cross-cutovers/service-start-interlock'
SERVICES=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-monitor.service')
AUX_UNITS=('tradingbot-watchdog.service','tradingbot-external-backup@.service','tradingbot-heartbeat.service')
ALL_UNITS=SERVICES+('tradingbot-paper-backup.service',)+AUX_UNITS
TIMERS=('tradingbot-paper-backup.timer','tradingbot-watchdog.timer','tradingbot-external-hourly.timer','tradingbot-external-daily.timer','tradingbot-heartbeat.timer')
START=('tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-spot.service','tradingbot-monitor.service')+TIMERS
INTERLOCK='cross:freeze:spotpilot-20261107c'

def command(*args):return subprocess.run(['systemctl',*args],check=True,capture_output=True,text=True,timeout=120)
def counts():
    names={'spot':b'trading_bot.app','futures':b'trading_bot.futures_app','cross':b'trading_bot.cross_app','margin':b'trading_bot.margin_app','monitor':b'trading_bot.paper_monitor'}
    result={k:0 for k in names}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:args=(proc/'cmdline').read_bytes().split(b'\0')
        except OSError:continue
        for name,arg in names.items():result[name]+=int(arg in args)
    return result

def on_hold():
    with closing(sqlite3.connect(FINANCIAL.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        def value(table,key):
            r=db.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
            return json.loads(r[0]) if r else None
        return (value('ledger_meta','deployment_hold') is True and value('runtime_meta','paused') is True and
                value('futures_meta','paused') is True and all(db.execute('SELECT paused FROM wallet_state WHERE wallet=?',(m,)).fetchone()==(1,) for m in ('futures_cross','margin_cross')))

def candidate_allowed(target):return str(target).startswith('/opt/trading-bot-spotpilot-')

def recover():
    if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise ValueError('Unexpected host')
    if not ARCHIVE.is_dir() or ARCHIVE.is_symlink():raise ValueError('Prepared archive missing')
    data=json.loads((ARCHIVE/'prepared.json').read_text());target=Path(data['target']).resolve(strict=True)
    if not candidate_allowed(target):raise ValueError('Unexpected candidate')
    if subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip()!=data['candidate']:raise ValueError('Candidate changed')
    sys.path.insert(0,str(target))
    from trading_bot.cross_unit_switch import hold_writer_locks,switch_unit_files
    from trading_bot.cross_service_interlock import release_interlock
    stages=ARCHIVE/'candidate-units';archive=ARCHIVE/'unit-archive'
    import hashlib
    plan=json.loads((stages/'manifest.json').read_text())
    if set(plan)!=set(ALL_UNITS):raise ValueError('Unit plan mismatch')
    desired={};old={}
    for name in ALL_UNITS:
        item=stages/name;live=UNITDIR/name;saved=ARCHIVE/'source-units'/name
        if any(p.is_symlink() or not p.is_file() for p in (item,live,saved)):
            raise ValueError('Invalid unit')
        desired[name]=item.read_bytes();old[name]=saved.read_bytes()
        if (hashlib.sha256(old[name]).hexdigest()!=plan[name]['source_sha256'] or
            hashlib.sha256(desired[name]).hexdigest()!=plan[name]['staged_sha256'] or
            live.read_bytes() not in (old[name],desired[name])):
            raise ValueError('Unexpected unit content')
    def identical(expected):return all((UNITDIR/name).read_bytes()==expected[name] for name in ALL_UNITS)
    state=json.loads((ARCHIVE/'switch-state').read_text())['stage'] if (ARCHIVE/'switch-state').exists() else 'not_started'
    if not on_hold():
        if not MARKER.exists() and identical(desired):
            proof=ARCHIVE/'release-proof.json'
            if proof.is_symlink() or not proof.is_file():raise ValueError('Candidate release not proven')
            released=json.loads(proof.read_text())
            enabled=released.get('enabled')
            if (released.get('operation')!='spotpilot:release:20261107c' or not isinstance(enabled,list)
                or len(enabled)!=len(set(enabled)) or any(x!='spot' for x in enabled)
                or ('spot' in enabled and data.get('paused',{}).get('spot') is not False)):
                raise ValueError('Unexpected release proof')
            print('REGULARAI_RECOVERY completed deployment; workers unchanged',flush=True);return
        if MARKER.exists() or not identical(old):raise ValueError('Unpaused recovery requires inspection')
        for unit in TIMERS:command('start',unit)
        print('REGULARAI_RECOVERY timers restored; workers unchanged',flush=True);return
    if not identical(old) and not identical(desired):
        if not MARKER.exists() or not (archive/'unit-switch.json').is_file() or not (ARCHIVE/'stopped/financial.db').is_file():raise ValueError('Mixed units lack recovery evidence')
        def stopped():return on_hold() and not any(counts().values())
        if not stopped():raise ValueError('Mixed units with active workers')
        with hold_writer_locks(FINANCIAL):
            switch_unit_files(UNITDIR,stages,archive,ALL_UNITS,assert_safe=stopped)
            command('daemon-reload')
    if not identical(old) and not identical(desired):raise ValueError('Units remain inconsistent')
    if MARKER.exists():
        if identical(desired) and any(counts().values()):raise ValueError('Interlocked candidate has active workers')
        expected=desired if identical(desired) else old
        release_interlock(UNITDIR,MARKER,SERVICES,INTERLOCK,verify_safe=lambda:on_hold() and identical(expected))
    command('daemon-reload')
    for unit in START:command('start',unit)
    print('REGULARAI_RECOVERY protection started on hold; manual verification required',flush=True)

if __name__=='__main__':recover()
