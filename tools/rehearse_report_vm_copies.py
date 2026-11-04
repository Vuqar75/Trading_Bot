"""Report routing and pause restoration on private coordinated VM copies.

No live service is stopped, no Telegram request is made and no trades are
created. The script verifies that report commands produce no financial events.
"""
import json,os,re,shutil,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path

if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test' or len(sys.argv)!=2 or not re.fullmatch('[0-9a-f]{40}',sys.argv[1]):raise SystemExit('Unexpected host or candidate')
expected=sys.argv[1];home=Path('/var/lib/tradingbot')
with tempfile.TemporaryDirectory(prefix='report-copy-rehearsal-') as temp:
    root=Path(temp);repo=root/'repo'
    subprocess.run(['git','clone','--quiet','--branch','research/margin-report-clarity','https://github.com/Vuqar75/Trading_Bot.git',str(repo)],check=True,timeout=90)
    subprocess.run(['git','-C',str(repo),'checkout','--quiet','--detach',expected],check=True,timeout=30)
    if subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()!=expected:raise ValueError('Candidate mismatch')
    sys.path.insert(0,str(repo))
    from trading_bot.cross_command_receiver import CrossCommandReceiver
    from trading_bot.cross_paper_book import CrossPaperBook
    from trading_bot.cross_worker import process_cross
    from trading_bot.margin_command_receiver import MarginCommandReceiver
    from trading_bot.margin_paper_book import MarginPaperBook
    from trading_bot.margin_worker import process_margin
    from trading_bot.paper_coordinated_backup import coordinated_backup
    from trading_bot.paper_shared_store import SharedPaperStore
    from trading_bot.report_update_guard import freeze_report_update,release_report_update
    from trading_bot.unified_queue import UnifiedQueue
    from trading_bot.unified_worker import process_market
    source=root/'source'
    coordinated_backup(home/'paper-financial/shared-paper.db',home/'Trading_Bot_paper_state/unified-commands.db',home/'Trading_Bot_paper_state/monitor/observation.db',source,timeout=15)
    active=root/'active';shutil.copytree(source,active);financial=active/'financial.db';queue=active/'queue.db'
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue)
    try:
        original={m:shared.snapshot(m)['position'] for m in ('spot','futures')}
        cross=CrossPaperBook(shared);cross_before=cross._positions();margin=MarginPaperBook(shared.ledger)
        events=[tuple(r) for r in shared.db.execute('SELECT id,fingerprint FROM ledger_events ORDER BY id')]
        frozen=freeze_report_update(shared,q,'reports:freeze:copy')
        receiver=MarginCommandReceiver(margin);cross_receiver=CrossCommandReceiver(cross)
        ident=int(q.state('offset'))
        for n,text in enumerate(('/mode margin_cross','/loans','/interest','/status margin_cross','/status')):q.receive(ident+n,True,text)
        now=time.time();process_margin(q,receiver,margin,shared,clock=lambda:now);process_cross(q,cross_receiver,cross,now=now,clock=lambda:now)
        class Store:
            def __init__(self,market):self.market=market
            def get(self,key):return shared._get(self.market,key)
        for market in ('spot','futures'):process_market(q,None,Store(market),market)
        def answer(update,market):
            row=q.db.execute('SELECT status,result FROM unified_jobs WHERE update_id=? AND market=?',(ident+update,market)).fetchone()
            if not row or row['status']!='DONE':raise ValueError('Report did not complete')
            return row['result']
        loans=answer(1,'margin_cross');interest=answer(2,'margin_cross');detail=answer(3,'margin_cross')
        if loans==interest or 'Начислено' not in interest or 'Собственный капитал' not in detail or 'Ошибки' not in detail:
            raise ValueError('Margin report outputs are not distinct')
        for market in ('spot','futures','futures_cross','margin_cross'):
            brief=answer(4,market).lower()
            if not all(word in brief for word in ('входы','позиций','обновление','состояние')) or len(brief)>=250:
                raise ValueError('Inconsistent general status')
        health=json.loads((home/'paper-financial/margin-health.json').read_text());checked=health['protection_checked_at']
        if health.get('error') or health.get('protection_fresh') is not True or not 0<=int(time.time()*1000)-checked<=120000:raise ValueError('Live margin protection not fresh')
        released=release_report_update(shared,q,'reports:release:copy',protection_checked_ms=checked)
        expected_enabled={m for m,v in frozen['paused'].items() if v is False}
        if set(released['enabled'])!=expected_enabled:raise ValueError('Original pauses not restored')
        if any(shared.snapshot(m)['position']!=original[m] for m in original) or cross._positions()!=cross_before:raise ValueError('Position changed on copied rehearsal')
        if events!=[tuple(r) for r in shared.db.execute('SELECT id,fingerprint FROM ledger_events ORDER BY id')]:raise ValueError('Report changed financial events')
        if not shared.ledger.reconcile()['balanced']:raise ValueError('Journal mismatch')
        print(json.dumps({'candidate':expected,'coordinated_private_copy':True,'distinct_margin_reports':True,
            'four_brief_statuses':'consistent','pause_restoration':'preserved','positions_funding_and_events':'unchanged',
            'journal_balanced':True,'network_or_telegram_calls':False,'live_services_changed':False},sort_keys=True),flush=True)
    finally:q.close();shared.close()
