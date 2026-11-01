"""VM cutover for the explicitly authorized paper-only wallet migration.

Run only after the full candidate suite and copy-only rehearsals. It is designed
for the existing TradingBot VM and writes a private stage manifest. A failed
cutover leaves entries paused; automatic rollback is attempted only if the
read-only financial rollback gate proves that no later financial event occurred.
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from decimal import Decimal,localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_rollback_guard import check_rollback
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report

HOME=Path('/var/lib/tradingbot');SPOT=HOME/'Trading_Bot_paper_state/runtime-paper.db';FUTURE=HOME/'Trading_Bot_futures_state/futures-paper.db'
QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
TARGET=HOME/'paper-financial/shared-paper.db';CODE=Path('/opt/trading-bot-wallet')
UNIT_DIR=Path('/etc/systemd/system')
UNITS=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-monitor.service','tradingbot-paper-backup.timer')
UNIT_FILES=('tradingbot-spot.service','tradingbot-futures.service','tradingbot-monitor.service','tradingbot-paper-backup.service','tradingbot-paper-backup.timer')
MAP={'tradingbot-spot.service':'vm-shared-spot-paper.service','tradingbot-futures.service':'vm-shared-futures-paper.service',
     'tradingbot-monitor.service':'vm-shared-paper-monitor.service','tradingbot-paper-backup.service':'vm-shared-paper-backup.service',
     'tradingbot-paper-backup.timer':'vm-shared-paper-backup.timer'}


def call(*args):return subprocess.run(args,check=True)
def active(name):return subprocess.run(['systemctl','is-active','--quiet',name]).returncode==0
def read_meta(path,table):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Source integrity failed')
        return {k:json.loads(v) for k,v in db.execute('SELECT key,value FROM '+table)}
def backup(source,dest):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=10)) as reader,closing(sqlite3.connect(dest)) as writer:
        reader.backup(writer)
        if writer.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Snapshot integrity failed')
    os.chmod(dest,0o600)
    with open(dest,'rb') as f:os.fsync(f.fileno())
def position_financial(pos):
    return {k:v for k,v in pos.items() if k!='funding_cursor'} if isinstance(pos,dict) else pos
def source_equity(spot,future):
    sm=spot.get('quote') or {};fm=future.get('quote') or {};now=time.time()
    if any(not q.get('price') or not 0<=now-q.get('time',0)<=120 for q in (sm,fm)):raise RuntimeError('Source quote stale')
    pos=future.get('position');sp=spot['position']
    with localcontext() as c:
        c.prec=120
        total=Decimal(spot['cash'])+Decimal(sp['quantity'])*Decimal(sm['price'])+Decimal(future['cash'])
        if pos:
            qty=Decimal(pos['quantity']);mark=Decimal(fm['price']);direction=1 if pos['direction']=='LONG' else -1
            gross=(mark-Decimal(pos['entry']))*qty*direction;fee=Decimal(future['identity']['fee_bps']);slip=Decimal(future['identity']['slippage_bps'])
            exitprice=mark*(1+(1 if pos['direction']=='SHORT' else -1)*slip/10000)
            total+=max(Decimal(0),Decimal(pos['margin'])+Decimal(pos['funding'])-Decimal(pos['entry_fee'])+gross-qty*exitprice*fee/10000-qty*mark*slip/10000)
        return total,max(sm['time'],fm['time'])
def health(path,mode):
    data=json.loads(path.read_text())
    if data.get('mode')!=mode or data.get('error') or data.get('blocked') or data.get('telegram_error') or not data.get('quote') or not 0<=time.time()-data['quote']['time']<=120:
        raise RuntimeError(mode+' health not ready')
    return data
def process_counts():
    out={'spot':0,'futures':0,'monitor':0}
    for pid in os.listdir('/proc'):
        if not pid.isdigit():continue
        try:
            a=Path('/proc',pid,'cmdline').read_bytes().split(b'\0')
            for key,arg in [('spot',b'trading_bot.app'),('futures',b'trading_bot.futures_app'),('monitor',b'trading_bot.paper_monitor')]:out[key]+=int(arg in a)
        except OSError:pass
    return out

def test_notification(queue):
    with closing(sqlite3.connect(queue,isolation_level=None,timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        ident=db.execute('INSERT INTO unified_outbox(message) VALUES(?)',('[Кошельки] Проверка единого paper-учёта: это тест, не сделка.',)).lastrowid
        db.commit()
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        with closing(sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            if not db.execute('SELECT 1 FROM unified_outbox WHERE id=?',(ident,)).fetchone():return
        time.sleep(1)
    raise RuntimeError('Telegram test notification was not acknowledged')


def release_entries(shared, queue, offset, previous):
    """Serialize restoration with durable receipt of manual pauses.

    Lock financial DB then command queue, the same order as close receipt and
    coordinated backup. Keep queue locked until the financial commit completes.
    """
    with closing(sqlite3.connect(queue,isolation_level=None,timeout=10)) as q:
        try:
            with shared.transaction():
                q.execute('BEGIN IMMEDIATE')
                if q.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():
                    raise RuntimeError('Queue has pending work')
                pauses=q.execute("SELECT command,market FROM unified_updates WHERE id>=? AND command IN ('/pause','/pause_all')",(offset,)).fetchall()
                manual={x for command,market in pauses for x in (('spot','futures') if command=='/pause_all' else (market,))}
                if not shared.deployment_hold():raise RuntimeError('Deployment hold not active')
                shared._validate_hold_change(False)
                shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','false')")
                for market in ('spot','futures'):
                    if not previous[market] and market not in manual:shared._set(market,'paused',False)
            return manual
        finally:
            if q.in_transaction:q.rollback()


def require_no_interrupted_cutover(home):
    for archive in home.glob('before-wallet-*'):
        if not archive.is_dir():raise RuntimeError('Unexpected cutover archive')
        try:stage=json.loads((archive/'manifest.json').read_text())['stage']
        except (OSError,ValueError,KeyError,TypeError):
            raise RuntimeError('Unresolved cutover archive requires manual inspection') from None
        if stage not in ('complete','rolled_back_paused'):
            raise RuntimeError('Interrupted cutover requires manual reconciliation')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--expected-commit',required=True);parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if not args.execute or os.geteuid()!=0:raise SystemExit('Root and explicit --execute are required')
    if subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected VM')
    head=subprocess.check_output(['git','-C',str(CODE),'rev-parse','HEAD'],text=True).strip()
    if head!=args.expected_commit or subprocess.check_output(['git','-C',str(CODE),'status','--porcelain'],text=True).strip():raise SystemExit('Unexpected candidate checkout')
    require_no_interrupted_cutover(HOME)
    for p in (SPOT,FUTURE,QUEUE,OBSERVER):
        if p.is_symlink() or not p.is_file() or HOME.resolve() not in p.resolve().parents:raise SystemExit('Unexpected source path')
        if any(Path(str(p)+suffix).exists() for suffix in ('-wal','-journal')):raise SystemExit('Source has active journal sidecar')
    if TARGET.exists():raise SystemExit('Shared financial destination already exists')
    if any(not active(u) for u in UNITS):raise SystemExit('Required original service not active')
    if process_counts()!={'spot':1,'futures':1,'monitor':1}:raise SystemExit('Unexpected active process count')
    for path,mode in [(SPOT.parent/'runtime-health.json','paper'),(FUTURE.parent/'futures-health.json','futures-paper')]:health(path,mode)
    if (OBSERVER.parent/'monitor-error.json').exists():raise SystemExit('Observer error present')
    old={'spot':read_meta(SPOT,'runtime_meta'),'futures':read_meta(FUTURE,'futures_meta')}
    for m in old.values():
        if m.get('blocked') or m.get('error') or m.get('close_request') or type(m.get('paused')) is not bool:raise SystemExit('Source not ready')
    if not old['spot']['position']['fees_known']:raise SystemExit('Spot fee valuation incomplete')
    with closing(sqlite3.connect(SPOT.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():raise SystemExit('Spot has unresolved orders')
    with closing(sqlite3.connect(QUEUE.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise SystemExit('Unified queue has pending work')
    tag=datetime.now(ZoneInfo('Asia/Baku')).strftime('%Y%m%d-%H%M%S');archive=HOME/('before-wallet-'+tag)
    archive.mkdir(mode=0o700)
    manifest={'candidate':head,'started_baku':datetime.now(ZoneInfo('Asia/Baku')).isoformat(),'stage':'preflight',
              'previous_paused':{label:meta['paused'] for label,meta in old.items()},'files':{}}
    def mark(stage,**extra):
        manifest.update(stage=stage,**extra);p=archive/'manifest.json';temp=archive/'.manifest.tmp'
        temp.write_text(json.dumps(manifest,sort_keys=True));os.chmod(temp,0o600)
        with open(temp,'rb') as stream:os.fsync(stream.fileno())
        os.replace(temp,p);fd=os.open(archive,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        print('CUTOVER_STAGE',stage,flush=True)
    for unit in UNIT_FILES:shutil.copy2(UNIT_DIR/unit,archive/unit)
    for label,p in [('spot',SPOT),('futures',FUTURE)]:
        st=p.stat();manifest['files'][label]={'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
    mark('preflight')
    stopped=False;new_started=False
    try:
        for label,path,table in [('spot',SPOT,'runtime_meta'),('futures',FUTURE,'futures_meta')]:
            with closing(sqlite3.connect(path,isolation_level=None,timeout=20)) as db:
                db.execute('BEGIN IMMEDIATE');meta={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM '+table)}
                if meta.get('blocked') or meta.get('error') or meta.get('close_request'):raise RuntimeError(label+' changed during pause')
                if meta.get('paused')!=manifest['previous_paused'][label] or position_financial(meta.get('position'))!=position_financial(old[label].get('position')):
                    raise RuntimeError(label+' pause or position changed during preflight')
                db.execute('UPDATE '+table+" SET value='true' WHERE key='paused'");db.commit()
            mark(label+'_paused')
        mark('entries_paused')
        stopped=True
        call('systemctl','stop','tradingbot-spot.service','tradingbot-futures.service')
        call('systemctl','stop','tradingbot-monitor.service','tradingbot-paper-backup.timer')
        if process_counts()!={'spot':0,'futures':0,'monitor':0}:raise RuntimeError('Original workers did not stop')
        spot=read_meta(SPOT,'runtime_meta');future=read_meta(FUTURE,'futures_meta')
        if not spot['paused'] or not future['paused'] or spot.get('close_request') or future.get('close_request'):raise RuntimeError('Post-stop state not paused')
        if position_financial(spot['position'])!=position_financial(old['spot']['position']) or position_financial(future['position'])!=position_financial(old['futures']['position']):
            raise RuntimeError('Position or funding changed during stop; preserve old state')
        with closing(sqlite3.connect(QUEUE.as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise RuntimeError('Queue became pending during stop')
            offset=int(db.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()[0])
        for p in (SPOT,FUTURE,QUEUE,OBSERVER):
            if any(Path(str(p)+s).exists() for s in ('-wal','-journal')):raise RuntimeError('Source journal sidecar remains')
        source_total,quote_time=source_equity(spot,future)
        for source,name in [(SPOT,'spot.db'),(FUTURE,'futures.db'),(QUEUE,'queue.db'),(OBSERVER,'observation.db')]:backup(source,archive/name)
        manifest.update(source_capital=str(source_total),spot_position=spot['position'],futures_position=future['position'],queue_offset=offset)
        mark('source_backups_complete')
        TARGET.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if TARGET.parent.is_symlink() or TARGET.parent.resolve().parent!=HOME.resolve():raise RuntimeError('Invalid financial directory')
        consolidate_snapshots(archive/'spot.db',archive/'futures.db',TARGET,captured_at=time.time())
        shared=SharedPaperStore(TARGET)
        try:
            shared.set_deployment_hold(True)
            report=live_report(shared,now=quote_time)
            if report['total'] is None or report['total']!=source_total:raise RuntimeError('Consolidated capital mismatch')
            if shared.snapshot('spot')['position']!=spot['position'] or shared.snapshot('futures')['position']!=future['position']:raise RuntimeError('Position changed during migration')
        finally:shared.close()
        user=subprocess.check_output(['id','-u','tradingbot'],text=True).strip();group=subprocess.check_output(['id','-g','tradingbot'],text=True).strip()
        os.chown(TARGET.parent,int(user),int(group));os.chmod(TARGET.parent,0o700);os.chown(TARGET,int(user),int(group));os.chmod(TARGET,0o600)
        mark('consolidated_verified')
        for p in (SPOT,FUTURE):os.chown(p,0,0);os.chmod(p,0o400)
        for target,source in MAP.items():shutil.copy2(CODE/'deploy'/source,UNIT_DIR/target)
        call('systemctl','daemon-reload')
        for name in UNIT_FILES:call('systemd-analyze','verify',str(UNIT_DIR/name))
        mark('old_writers_archived_and_units_installed')
        new_started=True
        for name in ('tradingbot-futures.service','tradingbot-spot.service','tradingbot-monitor.service','tradingbot-paper-backup.timer'):call('systemctl','start',name)
        mark('new_workers_started_on_hold')
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                if all(active(u) for u in UNITS) and process_counts()=={'spot':1,'futures':1,'monitor':1}:
                    sh=health(TARGET.parent/'runtime-health.json','paper');fh=health(TARGET.parent/'futures-health.json','futures-paper')
                    if sh.get('paused') is True and fh.get('paused') is True:break
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(1)
        else:raise RuntimeError('New workers did not become healthy on pause')
        if (OBSERVER.parent/'monitor-error.json').exists():raise RuntimeError('Observer reported error')
        shared=SharedPaperStore(TARGET)
        try:
            if not shared.deployment_hold():raise RuntimeError('Deployment hold not active')
            ns=shared.snapshot('spot');nf=shared.snapshot('futures')
            if Decimal(ns['cash'])!=Decimal(spot['cash']) or Decimal(nf['cash'])!=Decimal(future['cash']) or position_financial(ns['position'])!=position_financial(spot['position']) or position_financial(nf['position'])!=position_financial(future['position']):raise RuntimeError('Post-start financial state changed')
            if live_report(shared,now=time.time())['total'] is None:raise RuntimeError('Current capital is incomplete')
        finally:shared.close()
        import fcntl
        with open(str(TARGET)+'.delivery.lock','a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);raise RuntimeError('Futures relay lock is not held')
            except BlockingIOError:pass
        if int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))<102400:raise RuntimeError('Insufficient memory headroom')
        call('systemctl','start','tradingbot-paper-backup.service')
        test_notification(QUEUE)
        mark('healthy_on_hold_and_backup_complete')
        shared=SharedPaperStore(TARGET)
        try:manual=release_entries(shared,QUEUE,offset,manifest['previous_paused'])
        finally:shared.close()
        time.sleep(18)
        final={}
        for label,mode,file in [('spot','paper','runtime-health.json'),('futures','futures-paper','futures-health.json')]:
            d=health(TARGET.parent/file,mode);expected=manifest['previous_paused'][label] or label in manual
            if d['paused']!=expected:raise RuntimeError(label+' pause restoration mismatch')
            final[label]={'paused':d['paused'],'error':bool(d.get('error'))}
        mark('complete',finished_baku=datetime.now(ZoneInfo('Asia/Baku')).isoformat(),final=final)
        print('CUTOVER_COMPLETE',archive,'SOURCE_CAPITAL_USDT',str(source_total),'FINAL',json.dumps(final),flush=True)
    except BaseException as exc:
        print('CUTOVER_ERROR',type(exc).__name__,str(exc),flush=True)
        mark('failure',error_type=type(exc).__name__)
        if not stopped:
            for path,table in ((SPOT,'runtime_meta'),(FUTURE,'futures_meta')):
                try:
                    with closing(sqlite3.connect(path,isolation_level=None,timeout=10)) as db:
                        db.execute('BEGIN IMMEDIATE');db.execute('UPDATE '+table+" SET value='true' WHERE key='paused'");db.commit()
                except Exception as pause_error:
                    print('FAIL_SAFE_PAUSE_ERROR',type(pause_error).__name__,flush=True)
        if stopped:
            try:
                if new_started:subprocess.run(['systemctl','stop',*UNITS],check=False)
                safe=True
                if TARGET.exists():safe=check_rollback(TARGET,archive/'spot.db',archive/'futures.db')['safe_for_financial_rollback']
                if not safe:raise RuntimeError('Financial state advanced; preserve new data and do not restore old writers')
                for label,p in [('spot',SPOT),('futures',FUTURE)]:
                    info=manifest['files'][label];os.chown(p,info['uid'],info['gid']);os.chmod(p,info['mode'])
                for name in UNIT_FILES:shutil.copy2(archive/name,UNIT_DIR/name)
                call('systemctl','daemon-reload')
                for name in ('tradingbot-futures.service','tradingbot-spot.service','tradingbot-monitor.service','tradingbot-paper-backup.timer'):call('systemctl','start',name)
                mark('rolled_back_paused');print('SAFE_ROLLBACK_STARTED_PAUSED',flush=True)
            except BaseException as rollback_error:
                mark('manual_reconciliation_required',rollback_error=type(rollback_error).__name__)
                print('AUTOMATIC_ROLLBACK_REFUSED',type(rollback_error).__name__,str(rollback_error),flush=True)
        raise


if __name__=='__main__':main()
