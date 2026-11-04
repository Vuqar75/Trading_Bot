"""Verify, allocate and release an already switched paper deployment.

No production database is replaced. If any check fails the durable deployment
hold remains active; after allocation the old financial snapshot is never used
as a restore source. Outputs deliberately exclude Telegram configuration.
"""
import argparse,fcntl,json,os,shutil,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');ARCHIVE=HOME/'margin-cutovers/20261103b'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db';OBSERVER=HOME/'Trading_Bot_paper_state/monitor/observation.db'
UNITS={'spot':'tradingbot-spot.service','futures':'tradingbot-futures.service','cross':'tradingbot-cross.service','margin':'tradingbot-margin.service','monitor':'tradingbot-monitor.service'}
ALLOCATION='margin:initial:live-20261103b';RELEASE='margin:release:live-20261103b'


def prop(unit,key):
    return subprocess.check_output(['systemctl','show',unit,'--property='+key,'--value'],text=True,timeout=15).strip()


def _readonly(path):
    db=sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=10)
    db.row_factory=sqlite3.Row;db.execute('BEGIN');return db


def _counts():
    names={'spot':b'trading_bot.app','futures':b'trading_bot.futures_app','cross':b'trading_bot.cross_app',
           'margin':b'trading_bot.margin_app','monitor':b'trading_bot.paper_monitor'}
    result={key:0 for key in names}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:args=(proc/'cmdline').read_bytes().split(b'\0')
        except OSError:continue
        for name,arg in names.items():result[name]+=int(arg in args)
    return result


def _relay_locked():
    fd=os.open(str(FINANCIAL)+'.delivery.lock',os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        fcntl.flock(fd,fcntl.LOCK_UN);return False
    finally:os.close(fd)


def _history_preserved(baseline,current):
    with closing(_readonly(baseline)) as old,closing(_readonly(current)) as new:
        for table,order in (('ledger_events','id'),('ledger_postings','event_id,ordinal')):
            before={tuple(row) for row in old.execute('SELECT * FROM '+table+' ORDER BY '+order)}
            after={tuple(row) for row in new.execute('SELECT * FROM '+table+' ORDER BY '+order)}
            if not before.issubset(after):raise ValueError('Previous financial history changed')


def _positions(db):
    def get(table,key):
        row=db.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    spot=get('runtime_meta','position');future=get('futures_meta','position')
    if isinstance(future,dict):future={k:v for k,v in future.items() if k not in ('funding','funding_cursor')}
    cross=[{k:r[k] for k in r.keys() if k!='funding'} for r in db.execute('SELECT * FROM cross_positions ORDER BY symbol')]
    return {'spot':spot,'futures':future,'cross':cross}


def _market_health(target,*,held):
    from trading_bot.cross_paper_book import CrossPaperBook
    from trading_bot.margin_paper_book import MarginPaperBook
    from trading_bot.margin_interest_ledger import MarginInterestLedger
    from trading_bot.paper_wallet_ledger import PaperWalletLedger
    from trading_bot.paper_shared_store import SharedPaperStore
    now=time.time();pids={};health={}
    if _counts()!={k:1 for k in UNITS} or not _relay_locked():raise ValueError('Process or relay uniqueness not proven')
    if prop('tradingbot-paper-backup.timer','ActiveState')!='active':raise ValueError('Backup timer inactive')
    if prop(UNITS['margin'],'UnitFileState')!='enabled':raise ValueError('Margin service is not enabled')
    for market,unit in UNITS.items():
        expected_target=target if market=='spot' else Path('/opt/trading-bot-finalaudit-29d1fc6')
        if prop(unit,'ActiveState')!='active' or prop(unit,'WorkingDirectory')!=str(expected_target) or prop(unit,'NeedDaemonReload')!='no':raise ValueError('Service not running expected candidate')
        pid=int(prop(unit,'MainPID'));pids[market]=pid
        if pid<=1 or (Path('/proc')/str(pid)/'cwd').resolve()!=expected_target:raise ValueError('Process working directory mismatch')
        args=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
        if market=='spot' and b'--telegram' not in args:raise ValueError('Spot Telegram poller unavailable')
        if market=='margin' and b'trading_bot.margin_app' not in args:raise ValueError('Margin worker unavailable')
    if len(set(pids.values()))!=len(pids):raise ValueError('Services do not have distinct processes')
    available=int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:')))
    if available<102400:raise ValueError('Insufficient available memory')
    names={'spot':'runtime-health.json','futures':'futures-health.json','cross':'cross-health.json','margin':'margin-health.json'}
    for market,name in names.items():
        h=json.loads((FINANCIAL.parent/name).read_text());health[market]=h
        monotonic=int(prop(UNITS[market],'ExecMainStartTimestampMonotonic'))
        started=now-(time.monotonic()-monotonic/1_000_000)
        if not monotonic or not 0<=now-h['time']<=120 or h['time']<started-1 or h.get('error') or h.get('blocked'):
            raise ValueError('Stale or unhealthy paper service: '+market)
        if held and h.get('paused') is not True:raise ValueError('Market not paused: '+market)
        if market in ('spot','futures') and (not h.get('quote') or not 0<=now-h['quote'].get('time',0)<=120):raise ValueError('Market quote stale: '+market)
    if health['spot'].get('telegram_error'):raise ValueError('Telegram poller reports an error')
    if health['spot'].get('pending')!=0:raise ValueError('Spot has unsettled orders')
    cross=health['cross']
    if (cross.get('paused') is not True or cross.get('quotes_fresh') is not True or cross.get('protection_fresh') is not True or
        cross.get('funding_pages')!='loaded_available_pages' or cross.get('funding_paper_model')!='matches_paper_assumption' or
        cross.get('funding_publication_finality')!='not_guaranteed_by_source'):
        raise ValueError('Cross protection or funding not confirmed')
    margin=health['margin']
    if margin.get('quotes_fresh') is not True or margin.get('protection_fresh') is not True or margin.get('mode')!='spot-margin-cross-paper':
        raise ValueError('Margin protection or quote not confirmed')
    with closing(_readonly(ARCHIVE/'stopped/financial.db')) as old,closing(_readonly(FINANCIAL)) as db:
        if held and _positions(old)!=_positions(db):raise ValueError('Existing position changed during switching')
        shared=object.__new__(SharedPaperStore);shared.db=db;shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=db
        if not shared.ledger.reconcile()['balanced']:raise ValueError('Financial journal mismatch')
        for market in ('spot','futures'):shared._assert_consistent(market)
        if held and not shared.deployment_hold():raise ValueError('Deployment hold missing')
        if not held and shared.deployment_hold():raise ValueError('Deployment hold still active')
        book=object.__new__(CrossPaperBook);book.shared=shared;book.db=db;book.ledger=shared.ledger
        book._check();book._require_coverage(now);book._marks(book.stored_quotes(),now)
        mb=object.__new__(MarginPaperBook);mb.db=db;mb.ledger=shared.ledger
        mb.loans=object.__new__(MarginInterestLedger);mb.loans.db=db;mb.loans.ledger=shared.ledger;mb._check()
    _history_preserved(ARCHIVE/'stopped/financial.db',FINANCIAL)
    with closing(_readonly(QUEUE)) as db:
        if db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ValueError('Pending command requires processing')
    return {'time':now,'protection_checked_ms':margin['protection_checked_at'],'health_times':{m:health[m]['time'] for m in names},
            'paused':{m:health[m]['paused'] for m in names},'available_kib':available,'pids':pids}


def _backup(destination):
    from trading_bot.paper_coordinated_backup import coordinated_backup
    if destination.exists():return
    coordinated_backup(FINANCIAL,QUEUE,OBSERVER,destination,timeout=15)


def _emergency_hold():
    """Fail closed without changing any balance, position or trade history."""
    with closing(sqlite3.connect(FINANCIAL,isolation_level=None,timeout=10)) as db,closing(sqlite3.connect(QUEUE,isolation_level=None,timeout=10)) as q:
        try:
            db.execute('BEGIN IMMEDIATE');q.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','true')")
            for table in ('runtime_meta','futures_meta'):
                if db.execute('UPDATE '+table+" SET value='true' WHERE key='paused'").rowcount!=1:raise ValueError('Missing pause state')
            for wallet in ('spot','futures_isolated','futures_cross','margin_cross'):
                if db.execute('UPDATE wallet_state SET paused=1 WHERE wallet=?',(wallet,)).rowcount!=1:raise ValueError('Missing wallet pause')
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:
            if q.in_transaction:q.rollback()
    print('MARGIN_EMERGENCY_HOLD_APPLIED',flush=True)

