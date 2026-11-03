"""Run four paper workers under transient systemd with offline synthetic data.

Creates only private temporary files and transient services; fake Telegram and
socket denial prevent any network or access to the working paper databases.
Requires root on the explicitly named VM. No live service is stopped or edited.
"""
import ast,json,os,pwd,signal,sqlite3,subprocess,sys,tempfile,threading,time
from decimal import Decimal
from pathlib import Path

if os.geteuid()!=0 or subprocess.check_output(['hostname'],text=True).strip()!='tradingbot-paper-test':raise SystemExit('Unexpected host')
repo=Path(__file__).resolve().parents[1];sys.path.insert(0,str(repo))
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_rollout import freeze_margin_cutover,allocate_margin_once,release_margin_cutover
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_coordinated_backup import coordinated_backup
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue

tree=ast.parse((repo/'tools/rehearse_shared_processes.py').read_text())
spot_future=next(n.value.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='child' for x in n.targets))
cross_child='''import socket,sys,time
from unittest.mock import patch
import trading_bot.cross_app as app
class Fake:
 def schedule(self,symbol):
  now=int(time.time()*1000);return {'symbol':symbol,'mark':'100' if symbol=='BTCUSDT' else '200','server_ms':now,'next_due_ms':(now//28800000+1)*28800000,'interval_ms':28800000}
 def funding(self,*args):return []
 def candles(self,symbol,interval,server):
  period=900000;last=(server//period-1)*period
  return [(last-(39-i)*period,'100') for i in range(40)]
def deny(*args,**kwargs):raise AssertionError('network prohibited')
with patch.object(socket,'socket',deny),patch.object(app,'CrossPublicData',Fake):
 sys.argv=['cross-app','--db',sys.argv[1],'--queue',sys.argv[2],'--poll-seconds','5'];app.main()
'''
margin_child='''import socket,sys,time
from unittest.mock import patch
import trading_bot.margin_app as app
class Fake:
 HOST='https://api.binance.com'
 def snapshot(self,symbol,interval):
  now=int(time.time()*1000);period=900000;last=(now//period-1)*period;price='100' if symbol=='BTCUSDT' else '200'
  f={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':now,'notional_price':price,'notional_time_ms':now}
  return {'symbol':symbol,'price':price,'time_ms':now,'server_ms':now,'source':self.HOST,'price_kind':'last_public_trade','filters':f,'candles':[(last-(39-i)*period,'100') for i in range(40)]}
def deny(*args,**kwargs):raise AssertionError('network prohibited')
with patch.object(socket,'socket',deny),patch.object(app,'MarginSpotData',Fake):
 sys.argv=['margin-app','--db',sys.argv[1],'--queue',sys.argv[2],'--poll-seconds','5'];app.main()
'''

def systemctl(*args,check=True):
    r=subprocess.run(['systemctl',*args],capture_output=True,text=True,timeout=45)
    if check and r.returncode:raise RuntimeError('systemctl '+args[0]+' failed')
    return r.stdout.strip()

def wait(predicate,units,seconds=60):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if any(systemctl('is-active',u,check=False)!='active' for u in units):raise RuntimeError('Synthetic worker failed')
        try:
            if predicate():return
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError):pass
        time.sleep(.25)
    raise RuntimeError('Synthetic systemd rehearsal timed out')

with tempfile.TemporaryDirectory(prefix='margin-four-systemd-',dir='/var/lib/tradingbot') as tmp:
    root=Path(tmp);financial=root/'shared.db';queue_path=root/'queue.db';observation=root/'observation.db'
    spot=RuntimeStore(root/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(root/'futures.db')
    now=time.time();stamp=int(now*1000)
    future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),stamp,'rehearsal');original=future.get('position')
    with spot.transaction():spot.set('paused',False)
    with future.transaction():future.set('paused',True)
    spot.close();future.close();consolidate_snapshots(root/'spot.db',root/'futures.db',financial,captured_at=now)
    shared=SharedPaperStore(financial);q=UnifiedQueue(queue_path)
    with shared.transaction():
        shared._set('spot','quote',{'price':'100','time':now});shared._set('futures','quote',{'price':'100','time':now,'server_ms':stamp})
    cross=CrossPaperBook(shared)
    quotes={s:{'mark':'100','time':now,'funding_complete':True,'server_ms':stamp,'next_due_ms':(stamp//28800000+1)*28800000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
    cross.record_quotes(quotes,now=now)
    freeze=freeze_margin_cutover(shared,q,'margin:freeze:systemd-copy')
    MarginPaperBook(shared.ledger)
    # The monitor DB is independent and is only used for coordinated copy.
    db=sqlite3.connect(observation)
    try:db.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY)');db.commit()
    finally:db.close()
    scripts={'spot':spot_future,'futures':spot_future,'cross':cross_child,'margin':margin_child}
    for name,content in scripts.items():(root/(name+'.py')).write_text(content)
    user=pwd.getpwnam('tradingbot')
    for path in [root,*root.rglob('*')]:
        if not path.is_symlink():os.chown(path,user.pw_uid,user.pw_gid)
    units={name:'margin-rehearsal-'+str(os.getpid())+'-'+name+'.service' for name in scripts}
    try:
        for name,unit in units.items():
            args=['systemd-run','--quiet','--unit='+unit,'--property=User=tradingbot','--property=Group=tradingbot',
                  '--property=WorkingDirectory='+str(repo),'--property=NoNewPrivileges=true','--property=PrivateTmp=true',
                  '/usr/bin/python3','-B',str(root/(name+'.py'))]
            args+=([name,str(financial),str(queue_path)] if name in ('spot','futures') else [str(financial),str(queue_path)])
            subprocess.run(args,check=True,capture_output=True,timeout=30)
        def health(name):return json.loads((root/({'spot':'runtime','futures':'futures','cross':'cross','margin':'margin'}[name]+'-health.json')).read_text())
        def ready():
            h={name:health(name) for name in units}
            return all(x.get('paused') is True and not x.get('error') and 0<=time.time()-x['time']<=60 for x in h.values()) and h['cross']['protection_fresh'] and h['margin']['protection_fresh']
        wait(ready,units.values())
        allocation=allocate_margin_once(shared,q,'margin:initial:systemd-copy')
        if allocate_margin_once(shared,q,'margin:initial:systemd-copy')['event']!=allocation['event']:raise RuntimeError('Allocation replay mismatch')
        protection=health('margin')['protection_checked_at']
        released=release_margin_cutover(shared,q,'margin:release:systemd-copy',protection_checked_ms=protection)
        if set(released['enabled'])!={'spot','margin_cross'}:raise RuntimeError('Incorrect restored pause policy')
        def unpaused_expected():
            h={name:health(name) for name in units}
            return h['spot']['paused'] is False and h['futures']['paused'] is True and h['cross']['paused'] is True and h['margin']['paused'] is False
        wait(unpaused_expected,units.values())
        samples=[];stop=threading.Event()
        def memory():
            while not stop.is_set():
                samples.append(int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:'))));time.sleep(.05)
        watcher=threading.Thread(target=memory);watcher.start()
        try:coordinated_backup(financial,queue_path,observation,root/'backup',timeout=8)
        finally:stop.set();watcher.join()
        rss={}
        for name,unit in units.items():
            pid=int(systemctl('show',unit,'--property=MainPID','--value'))
            rss[name]=int(next(s.split()[1] for s in Path('/proc',str(pid),'status').read_text().splitlines() if s.startswith('VmRSS:')))
        for unit in units.values():systemctl('restart',unit)
        wait(ready,units.values())
        restored=shared.snapshot('futures')['position']
        for key in ('id','direction','quantity','entry','margin','funding'):
            if restored.get(key)!=original.get(key):raise RuntimeError('Synthetic existing position changed')
        if not shared.ledger.reconcile()['balanced']:raise RuntimeError('Synthetic journal mismatch')
        print(json.dumps({'four_real_systemd_processes':'ok','network':'blocked','original_pauses_restored':True,
            'cross_remained_paused':True,'restart_pauses_all':True,'copy_transfer_replay':'one_event',
            'position_and_funding_preserved':True,'backup_integrity':'ok','min_available_kib':min(samples),
            'memory_samples':len(samples),'rss_kib':rss,'production_changed':False},sort_keys=True),flush=True)
    finally:
        for unit in units.values():systemctl('stop',unit,check=False)
        for unit in units.values():systemctl('reset-failed',unit,check=False)
        q.close();shared.close()
