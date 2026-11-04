"""Private copy-only restore, margin and watchdog checks on the trading VM."""
import json,os,pwd,re,shutil,sqlite3,subprocess,sys,tempfile,time
from contextlib import closing
from pathlib import Path

if os.geteuid()!=0 or len(sys.argv)!=2 or not re.fullmatch('[0-9a-f]{40}',sys.argv[1]):raise SystemExit('Exact commit and root required')
if subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected host')
commit=sys.argv[1];home=Path('/var/lib/tradingbot')
with tempfile.TemporaryDirectory(prefix='diagnostics-copy-') as tmp:
    root=Path(tmp);repo=root/'repo'
    subprocess.run(['git','clone','--quiet','--branch','research/paper-resilience-hardening','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=90)
    subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',commit],check=True,timeout=30)
    sys.path.insert(0,str(repo))
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.paper_external_backup import prepare,inspect_restore
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.margin_paper_book import MarginPaperBook
    source=root/'source'
    coordinated_backup(home/'paper-financial/shared-paper.db',home/'Trading_Bot_paper_state/unified-commands.db',home/'Trading_Bot_paper_state/monitor/observation.db',source,timeout=15)
    stage=root/'stage';stage.mkdir(mode=0o700)
    prepared=prepare(source/'financial.db',source/'queue.db',source/'observation.db',stage,commit=commit)
    restored=root/'restored';result=inspect_restore(prepared['path'],restored,expected_sha256=prepared['sha256'])
    for name in ('financial.db','queue.db','observation.db'):
        with closing(sqlite3.connect(source/name)) as a,closing(sqlite3.connect(restored/name)) as b:
            if list(a.iterdump())!=list(b.iterdump()):raise ValueError('Restore content mismatch: '+name)
    shared=SharedPaperStore(restored/'financial.db')
    try:
        book=MarginPaperBook(shared.ledger)
        if book._positions() or book.db.execute("SELECT 1 FROM margin_loans WHERE status='OPEN'").fetchone():raise ValueError('Copied margin not flat')
        def quotes(t):
            f={'lot_rules':[{'min':'.0001','max':'100','step':'.0001'}],'min_notional':'0','max_notional':'0','observed_ms':t}
            return {symbol:{'price':price,'time_ms':t,'server_ms':t,'source':'fixture','price_kind':'last_public_trade','filters':f} for symbol,price in (('BTCUSDT','100'),('ETHUSDT','200'))}
        now=int(time.time()*1000)+1000;q=quotes(now);book.record_quotes(q,now_ms=now)
        with book.ledger.transaction():book.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        book.open('copy:long','copy:long','BTCUSDT','LONG','.09','5',2,q,now_ms=now)
        book.open('copy:short','copy:short','ETHUSDT','SHORT','.04','5',2,q,now_ms=now)
        later=now+3600001;q=quotes(later);book.record_quotes(q,now_ms=later)
        book.close('copy:close-long','BTCUSDT','copy:long','.09',q,now_ms=later)
        book.close('copy:close-short','ETHUSDT','copy:short','.04',q,now_ms=later)
        book._check()
        if book._positions() or book.db.execute("SELECT 1 FROM margin_loans WHERE status='OPEN'").fetchone() or not shared.ledger.reconcile()['balanced']:raise ValueError('Margin copy did not reconcile or repay')
        if not book.db.execute('SELECT 1 FROM margin_interest_periods WHERE period>=1').fetchone():raise ValueError('No accrued interest in copy')
    finally:shared.close()
    # Run the watchdog against another private copy. No Telegram poller is
    # started and the new messages remain inside the isolated copied outbox.
    private=root/'home';(private/'paper-financial').mkdir(parents=True);(private/'Trading_Bot_paper_state/monitor').mkdir(parents=True)
    shutil.copyfile(source/'financial.db',private/'paper-financial/shared-paper.db');shutil.copyfile(source/'queue.db',private/'Trading_Bot_paper_state/unified-commands.db')
    shutil.copyfile(source/'observation.db',private/'Trading_Bot_paper_state/monitor/observation.db')
    for name in ('runtime-health.json','futures-health.json','cross-health.json','margin-health.json'):
        shutil.copyfile(home/'paper-financial'/name,private/'paper-financial'/name)
    account=pwd.getpwnam('tradingbot')
    for p in [root,*root.rglob('*')]:
        if not p.is_symlink():os.chown(p,account.pw_uid,account.pw_gid)
    unit='paper-diagnostic-copy-'+str(os.getpid())
    subprocess.run(['systemd-run','--quiet','--wait','--collect','--pipe','--unit='+unit,'--property=User=tradingbot',
                    '--property=Group=tradingbot','--property=WorkingDirectory='+str(repo),'/usr/bin/python3','-B',
                    str(repo/'deploy/paper_watchdog.py'),'--home',str(private)],check=True,timeout=90)
    with closing(sqlite3.connect((private/'paper-financial/shared-paper.db').as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM paper_watchdog_state WHERE key='incidents'").fetchone():raise ValueError('Copy watchdog did not persist')
        if not db.execute("SELECT 1 FROM futures_outbox WHERE message LIKE '[Контроль системы]%' LIMIT 1").fetchone():raise ValueError('Copy watchdog did not enqueue')
    print(json.dumps({'candidate':commit,'source':'private coordinated copy','backup_bytes':prepared['size'],
      'restore_content_and_ledger':'verified','copied_margin_long_short_interest_close':'verified',
      'watchdog_systemd_on_copy':'verified','telegram_poller_started':False,'live_databases_modified':False},sort_keys=True),flush=True)
