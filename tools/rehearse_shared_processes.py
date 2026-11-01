"""Run two real paper entry points against temporary copies with network blocked.

All three paths must be under a dedicated directory in the system temp directory.
This script mutates its inputs. It never reads real Telegram credentials or sends
network requests, and intentionally exercises pause/resume on the temporary copy.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

root=Path(sys.argv[1]).resolve(strict=True)
tmp=Path(tempfile.gettempdir()).resolve(strict=True)
if root==tmp or tmp not in root.parents:
    raise SystemExit('A dedicated directory under the system temporary directory is required')
financial=root/'shared.db';queue=root/'queue.db';observation=root/'observation.db'
for p in (financial,queue,observation):
    if p.is_symlink() or not p.is_file() or p.resolve().parent!=root:
        raise SystemExit('Expected three regular temporary files')

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_coordinated_backup import coordinated_backup
from trading_bot.unified_queue import UnifiedQueue

child=r'''
import json,os,socket,sqlite3,sys,time
from decimal import Decimal
from unittest.mock import patch
from trading_bot.app import TelegramTransport
from trading_bot.testnet_observer import SpotTestnetReader
from trading_bot.futures_data import FuturesData
mode,db,queue=sys.argv[1:]
os.environ['UNIFIED_PAPER_QUEUE']=queue
os.environ['RUNTIME_TELEGRAM_BOT_TOKEN']='FAKE'
os.environ['RUNTIME_TELEGRAM_OWNER_ID']='1'
with sqlite3.connect(db) as c:
 row=c.execute("select value from futures_meta where key='position'").fetchone()
 pos=json.loads(row[0]) if row else None
 mark=Decimal(pos['entry']) if pos else Decimal('100')
def spot_get(self,path,params=None):
 now=int(time.time()*1000);period=900000;last=(now//period-1)*period
 if path.endswith('/time'):return {'serverTime':now}
 if path.endswith('/price'):return {'symbol':'BTCUSDT','price':str(mark)}
 if path.endswith('/exchangeInfo'):return {'symbols':[{'symbol':'BTCUSDT','baseAsset':'BTC','quoteAsset':'USDT','status':'TRADING','orderTypes':['MARKET'],'filters':[{'filterType':'LOT_SIZE','stepSize':'0.00001','minQty':'0.00001','maxQty':'100'},{'filterType':'MARKET_LOT_SIZE','stepSize':'0','minQty':'0','maxQty':'10'},{'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]}
 if path.endswith('/klines'):return [[last-(39-i)*period,0,0,0,str(mark),0,last-(39-i)*period+period-1] for i in range(40)]
 raise AssertionError('Unexpected fake Spot endpoint')
def future_candles(self,interval,server):
 period={'1m':60000,'5m':300000,'15m':900000,'1h':3600000}[interval];last=(server//period-1)*period
 return [(last-(39-i)*period,mark) for i in range(40)]
def fake_telegram(self,method,data):return [] if method=='getUpdates' else {}
def deny_network(*args,**kwargs):raise AssertionError('Network prohibited in rehearsal')
with patch.object(socket,'socket',deny_network),patch.object(SpotTestnetReader,'get',spot_get),patch.object(FuturesData,'mark',lambda self:(mark,int(time.time()*1000))),patch.object(FuturesData,'funding',lambda self,*a:[]),patch.object(FuturesData,'candles',future_candles),patch.object(TelegramTransport,'call',fake_telegram):
 if mode=='spot':
  import trading_bot.app as a
  sys.argv=['app','--mode','paper','--interval','15m','--amount','10','--telegram','--allow-paper-signal','--shared-paper','--db',db,'--futures-outbox-db',db,'--poll-seconds','5'];a.main()
 else:
  import trading_bot.futures_app as a
  sys.argv=['futures','--db',db,'--margin','10','--fee-bps','5','--slippage-bps','2','--maintenance-bps','50','--allow-paper-signal','--shared-paper','--poll-seconds','5'];a.main()
'''

def wait_for(predicate,seconds=25):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        if any(p.poll() is not None for p in processes):raise RuntimeError('A rehearsal worker exited')
        if predicate():return
        time.sleep(.2)
    raise RuntimeError('Rehearsal timed out')

def health(name):
    path=root/('runtime-health.json' if name=='spot' else 'futures-health.json')
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}

def fresh_paused(paused):
    a,b=health('spot'),health('futures')
    return all(d.get('paused') is paused and not d.get('error') and d.get('quote') and 0<=time.time()-d['quote']['time']<30 for d in (a,b))

def start():
    logs=[];ps=[]
    for mode in ('spot','futures'):
        log=open(root/(mode+'-rehearsal.log'),'a');logs.append(log)
        ps.append(subprocess.Popen([sys.executable,'-B','-c',child,mode,str(financial),str(queue)],cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=log))
    return ps,logs

def stop():
    for p in processes:
        if p.poll() is None:p.send_signal(signal.SIGTERM)
    for p in processes:
        try:p.wait(timeout=20)
        except subprocess.TimeoutExpired:p.kill();p.wait();raise RuntimeError('Worker did not stop gracefully')
    for f in logs:f.close()

shared=SharedPaperStore(financial)
try:
    before=shared.snapshot('futures')['position']
    shared.set_deployment_hold(True)
    processes,logs=start()
    try:
        wait_for(lambda:fresh_paused(True))
        for p in processes:p.send_signal(signal.SIGUSR1)
        time.sleep(7)
        if not fresh_paused(True):raise RuntimeError('Deployment hold did not preserve pause')
        q=UnifiedQueue(queue)
        try:
            ident=int(q.state('offset'));q.receive(ident,True,'/wallets')
            wait_for(lambda:q.db.execute('SELECT status FROM unified_jobs WHERE update_id=? AND market=?',(ident,'wallet')).fetchone()[0]=='DONE')
        finally:q.close()
        coordinated_backup(financial,queue,observation,root/'coordinated-backup',timeout=5)
        shared.set_deployment_hold(False)
        for p in processes:p.send_signal(signal.SIGUSR1)
        wait_for(lambda:fresh_paused(False))
    finally:stop()
    processes,logs=start()
    try:
        wait_for(lambda:fresh_paused(True))
        after=shared.snapshot('futures')['position']
        fields=('id','direction','quantity','entry','margin','leverage','funding')
        if (before is None)!=(after is None) or (before and any(before.get(k)!=after.get(k) for k in fields)):
            raise RuntimeError('Futures position/funding changed during rehearsal')
        if not shared.ledger.reconcile()['balanced']:raise RuntimeError('Rehearsal journal mismatch')
    finally:stop()
    print('TWO_WORKERS_HOLD_QUEUE_BACKUP_RESTART_OK',flush=True)
    print('FUTURES_POSITION_AND_FUNDING_PRESERVED',True,flush=True)
finally:shared.close()
