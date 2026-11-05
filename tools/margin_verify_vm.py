"""Verify, allocate and release an already switched paper deployment.

No production database is replaced. If any check fails the durable deployment
hold remains active; after allocation the old financial snapshot is never used
as a restore source. Outputs deliberately exclude Telegram configuration.
"""
import argparse,fcntl,json,math,os,shutil,sqlite3,subprocess,sys,tempfile,time
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


def health_record_problem(health,*,checked_at,started_at):
    """Classify one already-read record using a time obtained after that read."""
    if not isinstance(health,dict):return 'missing'
    stamp=health.get('time')
    if type(stamp) not in (int,float) or not math.isfinite(stamp):return 'invalid_time'
    if stamp>checked_at:return 'future_time'
    if checked_at-stamp>120:return 'stale_time'
    if stamp<started_at-1:return 'before_service_start'
    if health.get('error') or health.get('blocked'):return 'error_or_blocked'
    return None


def cross_refresh_in_progress(value,*,checked_at,pid):
    """Only classify a current worker's marker; this never establishes health."""
    if not isinstance(value,dict) or value.get('phase')!='refreshing' or value.get('pid')!=pid:return False
    stamp=value.get('observed_at');start=value.get('started_at')
    return (type(stamp) in (int,float) and type(start) in (int,float) and
            math.isfinite(stamp) and math.isfinite(start) and start<=stamp<=checked_at and checked_at-stamp<=120)


def _market_health(target,*,held):
    from trading_bot.cross_paper_book import CrossPaperBook
    from trading_bot.margin_paper_book import MarginPaperBook
    from trading_bot.margin_interest_ledger import MarginInterestLedger
    from trading_bot.paper_wallet_ledger import PaperWalletLedger
    from trading_bot.paper_shared_store import SharedPaperStore
    pids={};health={}
    if _counts()!={k:1 for k in UNITS} or not _relay_locked():raise ValueError('Process or relay uniqueness not proven')
    if prop('tradingbot-paper-backup.timer','ActiveState')!='active':raise ValueError('Backup timer inactive')
    if prop(UNITS['margin'],'UnitFileState')!='enabled':raise ValueError('Margin service is not enabled')
    for market,unit in UNITS.items():
        if prop(unit,'ActiveState')!='active' or prop(unit,'WorkingDirectory')!=str(target) or prop(unit,'NeedDaemonReload')!='no':raise ValueError('Service not running expected candidate')
        pid=int(prop(unit,'MainPID'));pids[market]=pid
        if pid<=1 or (Path('/proc')/str(pid)/'cwd').resolve()!=target:raise ValueError('Process working directory mismatch')
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
        now=time.time()
        started=now-(time.monotonic()-monotonic/1_000_000)
        reason=health_record_problem(h,checked_at=now,started_at=started)
        if not monotonic or reason:
            raise ValueError('Stale or unhealthy paper service: '+market+' ['+(reason or 'unknown_start')+']')
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
        now=time.time()
        book._check();book._require_coverage(now)
        try:book._marks(book.stored_quotes(),now)
        except ValueError as exc:
            if str(exc).startswith('Incomplete cross data:'):
                try:marker=json.loads((FINANCIAL.parent/'cross-sync-diagnostic.json').read_text())
                except (OSError,ValueError,TypeError):marker=None
                if cross_refresh_in_progress(marker,checked_at=time.time(),pid=pids['cross']):
                    raise ValueError('Cross data synchronization in progress; risk data unconfirmed') from None
            raise
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


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=('ready','telegram','allocate','release','postcheck'));args=parser.parse_args()
    if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected host')
    prepared=json.loads((ARCHIVE/'prepared.json').read_text());candidate=prepared['candidate']
    if len(candidate)!=40 or any(c not in '0123456789abcdef' for c in candidate):raise ValueError('Invalid candidate')
    target=Path('/opt/trading-bot-margin-'+candidate[:7]).resolve(strict=True)
    if subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip()!=candidate:raise ValueError('Candidate mismatch')
    if json.loads((ARCHIVE/'switch-state').read_text())['stage']!='candidate_services_started_on_hold':raise ValueError('Switch not completed on hold')
    sys.path.insert(0,str(target))
    from trading_bot.cross_unit_switch import _write
    from trading_bot.margin_rollout import allocate_margin_once,release_margin_cutover
    from trading_bot.margin_valuation_verifier import verify_allocation_value
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.paper_wallet_live_report import live_report
    from trading_bot.unified_queue import UnifiedQueue
    if args.phase=='ready':
        deadline=time.monotonic()+150;previous={};cycles=0;last_error='not checked'
        while time.monotonic()<deadline and cycles<3:
            try:
                current=_market_health(target,held=True)
                stamps=current['health_times']
                if not previous or all(stamps[k]>previous[k] for k in stamps):
                    cycles+=1;previous=stamps;print('MARGIN_HEALTH_CYCLE',cycles,flush=True)
            except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as exc:last_error=type(exc).__name__+': '+str(exc)
            if cycles<3:time.sleep(2)
        if cycles<3:raise ValueError('Healthy cycles not proven: '+last_error)
        current.update({'candidate':candidate,'cycles':cycles})
        _write(ARCHIVE/'ready.json',json.dumps(current,sort_keys=True).encode(),0o600)
        print('MARGIN_READY_ON_HOLD',flush=True);return
    if args.phase=='telegram':
        if not (ARCHIVE/'ready.json').is_file():raise ValueError('Paused health verification required')
        _market_health(target,held=True)
        shared=SharedPaperStore(FINANCIAL)
        try:
            with shared.transaction():
                row=shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_telegram_probe'").fetchone()
                if row:ident=json.loads(row[0])['outbox_id']
                else:
                    cur=shared.db.execute('INSERT INTO futures_outbox(message) VALUES(?)',('[Spot margin cross • PAPER] Проверка связи после обновления. Входы остаются на паузе.',))
                    ident=cur.lastrowid
                    shared.db.execute("INSERT INTO ledger_meta VALUES('margin_telegram_probe',?)",(json.dumps({'outbox_id':ident},sort_keys=True),))
            deadline=time.monotonic()+75
            while shared.db.execute('SELECT 1 FROM futures_outbox WHERE id=?',(ident,)).fetchone():
                if time.monotonic()>=deadline:raise ValueError('Telegram API delivery not confirmed')
                time.sleep(2)
            if json.loads((FINANCIAL.parent/'runtime-health.json').read_text()).get('telegram_error'):
                raise ValueError('Telegram poller reports an error')
        finally:shared.close()
        _write(ARCHIVE/'telegram-proof.json',json.dumps({'candidate':candidate,'api_accepted':True,'at':time.time()},sort_keys=True).encode(),0o600)
        print('MARGIN_TELEGRAM_API_ACCEPTED; OWNER_RECEIPT_NOT_CONFIRMED',flush=True);return
    if args.phase=='allocate':
        if not (ARCHIVE/'ready.json').is_file():raise ValueError('Paused health verification required')
        telegram=json.loads((ARCHIVE/'telegram-proof.json').read_text())
        if telegram.get('candidate')!=candidate or telegram.get('api_accepted') is not True:raise ValueError('Telegram API verification required')
        _market_health(target,held=True)
        before=ARCHIVE/'pre-allocation';after=ARCHIVE/'post-allocation'
        if not before.exists():
            deadline=time.monotonic()+60
            while True:
                with tempfile.TemporaryDirectory(prefix='.pre-allocation-',dir=ARCHIVE) as tmp:
                    snap=Path(tmp)/'backup';_backup(snap)
                    check=Path(tmp)/'check.db';shutil.copyfile(snap/'financial.db',check)
                    store=SharedPaperStore(check)
                    try:
                        when=json.loads((snap/'manifest.json').read_text())['captured_at']
                        value=live_report(store,now=when)
                        valid=value['total'] is not None and value['valuation_ok'] is True
                    finally:store.close()
                    if valid:os.rename(snap,before);break
                if time.monotonic()>=deadline:raise ValueError('Could not capture complete fixed-price valuation')
                time.sleep(2)
        shared=SharedPaperStore(FINANCIAL);queue=UnifiedQueue(QUEUE)
        try:allocation=allocate_margin_once(shared,queue,ALLOCATION)
        finally:queue.close();shared.close()
        _backup(after)
        when=json.loads((before/'manifest.json').read_text())['captured_at']
        proof=verify_allocation_value(before/'financial.db',after/'financial.db',allocation['token'],now=when)
        proof.update({'candidate':candidate,'event':allocation['event'],'token':allocation['token'],'verified_at':time.time()})
        _write(ARCHIVE/'allocation-proof.json',json.dumps(proof,sort_keys=True).encode(),0o600)
        print(json.dumps({'allocation':'one_virtual_50_USDT','capital_before':proof['capital_before'],'capital_after':proof['capital_after'],'same_fixed_quotes':True},sort_keys=True),flush=True);return
    if args.phase=='release':
        proof=json.loads((ARCHIVE/'allocation-proof.json').read_text())
        if proof.get('candidate')!=candidate or proof.get('capital_before')!=proof.get('capital_after'):raise ValueError('Allocation proof missing')
        current=_market_health(target,held=True)
        try:
            shared=SharedPaperStore(FINANCIAL);queue=UnifiedQueue(QUEUE)
            try:
                allocation=json.loads(shared.db.execute("SELECT value FROM ledger_meta WHERE key='margin_cutover_allocation'").fetchone()[0])
                if allocation['event']!=proof['event'] or allocation['token']!=proof['token']:raise ValueError('Allocation proof does not match journal')
                result=release_margin_cutover(shared,queue,RELEASE,protection_checked_ms=current['protection_checked_ms'])
            finally:queue.close();shared.close()
            _write(ARCHIVE/'release-proof.json',json.dumps(result,sort_keys=True).encode(),0o600)
        except BaseException:
            _emergency_hold();raise
        print(json.dumps({'enabled':result['enabled'],'manual_pauses':result['manual_pauses'],'cross_remains_paused':True},sort_keys=True),flush=True);return
    if not (ARCHIVE/'release-proof.json').is_file():raise ValueError('Release has not completed')
    deadline=time.monotonic()+90;last='not checked'
    while True:
        try:current=_market_health(target,held=False);break
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as exc:
            last=type(exc).__name__+': '+str(exc)
            if time.monotonic()>=deadline:
                _emergency_hold();raise ValueError('Post-release health not proven: '+last)
            time.sleep(2)
    try:_backup(ARCHIVE/'post-release')
    except BaseException:_emergency_hold();raise
    print(json.dumps({'paused':current['paused'],'available_kib':current['available_kib'],'journal_and_positions':'verified','backup':'post-release','telegram_poller_and_relay':'healthy','telegram_owner_delivery':'not_yet_confirmed'},sort_keys=True),flush=True)

if __name__=='__main__':main()
