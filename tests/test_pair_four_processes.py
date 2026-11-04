import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_pair_snapshots import pair_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


CHILD=r'''
import os,socket,sys,time
from decimal import Decimal
from unittest.mock import patch
from trading_bot.app import TelegramTransport
from trading_bot.testnet_observer import SpotTestnetReader
from trading_bot.futures_data import FuturesData
from trading_bot.margin_spot_data import MarginSpotData
mode,db,queue=sys.argv[1:]
os.environ['UNIFIED_PAPER_QUEUE']=queue;os.environ['RUNTIME_TELEGRAM_BOT_TOKEN']='FAKE';os.environ['RUNTIME_TELEGRAM_OWNER_ID']='1'
def info(symbol):
 row={'symbol':symbol,'baseAsset':symbol[:-4],'quoteAsset':'USDT','marginAsset':'USDT','contractType':'PERPETUAL','status':'TRADING','isSpotTradingAllowed':True,'orderTypes':['MARKET'],
      'filters':[{'filterType':'LOT_SIZE','minQty':'.001','maxQty':'1000','stepSize':'.001'},
                 {'filterType':'MARKET_LOT_SIZE','minQty':'0','maxQty':'0','stepSize':'0'},
                 {'filterType':'MIN_NOTIONAL','minNotional':'5','avgPriceMins':0}]}
 return row
def candles(now):
 period=900000;last=(now//period-1)*period
 return [[last-(39-i)*period,0,0,0,'100',0,last-(38-i)*period-1] for i in range(40)]
def spot_get(self,path,params=None,*args,**kwargs):
 params=params or {};symbol=params.get('symbol','BTCUSDT');now=int(time.time()*1000)
 if path.endswith('/time'):return {'serverTime':now}
 if path.endswith('/exchangeInfo'):return {'symbols':[info(symbol)]}
 if path.endswith('/ticker/price'):return {'symbol':symbol,'price':'100'}
 if path.endswith('/trades'):return [{'id':1,'time':now-10,'price':'100'}]
 if path.endswith('/referencePrice'):return {'symbol':symbol,'timestamp':now-10,'referencePrice':'100'}
 if path.endswith('/klines'):return candles(now)
 raise AssertionError('unexpected spot path '+path)
def future_get(self,path,params=None):
 params=params or {};symbol=params.get('symbol',self.pair_symbol);now=int(time.time()*1000)
 if path.endswith('/time'):return {'serverTime':now}
 if path.endswith('/exchangeInfo'):return {'symbols':[info(x) for x in ('BTCUSDT','ETHUSDT','SOLUSDT')]}
 if path.endswith('/premiumIndex'):return {'symbol':symbol,'time':now,'markPrice':'100','nextFundingTime':now+28800000}
 if path.endswith('/fundingInfo') or path.endswith('/fundingRate'):return []
 if path.endswith('/klines'):return candles(now)
 raise AssertionError('unexpected futures path '+path)
def telegram(self,method,data):
 if method=='getUpdates':
  with open(os.path.join(os.path.dirname(db),'pollers.log'),'a') as f:f.write(str(os.getpid())+'\n')
  return []
 return {}
def no_network(*args,**kwargs):raise AssertionError('network prohibited')
with patch.object(socket,'socket',no_network),patch.object(SpotTestnetReader,'get',spot_get),patch.object(MarginSpotData,'get',spot_get),patch.object(FuturesData,'get',future_get),patch.object(TelegramTransport,'call',telegram):
 if mode=='spot':
  import trading_bot.app as app
  sys.argv=['spot','--shared-paper','--experimental-pair','--telegram','--db',db,'--futures-outbox-db',db,'--poll-seconds','5'];app.main()
 elif mode=='futures':
  import trading_bot.futures_app as app
  sys.argv=['futures','--shared-paper','--experimental-pair','--db',db,'--poll-seconds','5'];app.main()
 elif mode=='cross':
  import trading_bot.cross_app as app
  sys.argv=['cross','--experimental-pair','--db',db,'--queue',queue,'--poll-seconds','5'];app.main()
 else:
  import trading_bot.margin_app as app
  sys.argv=['margin','--experimental-pair','--db',db,'--queue',queue,'--poll-seconds','5'];app.main()
'''


class PairFourProcessesTests(unittest.TestCase):
    def test_four_processes_pair_selection_and_restart(self):
        if os.name!='posix':self.skipTest('Linux process locking required')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);financial=root/'shared.db';queue_path=root/'queue.db'
            identity={'schema':1,'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10','account':'paper'}
            s=RuntimeStore(root/'spot.db',identity);s.close();f=FuturesStore(root/'future.db');f.close()
            consolidate_snapshots(root/'spot.db',root/'future.db',financial,captured_at=1)
            shared=SharedPaperStore(financial);CrossPaperBook(shared);MarginPaperBook(shared.ledger)
            q=UnifiedQueue(queue_path,allow_pair_experiment=True)
            procs=[];logs=[]
            def start(mode):
                log=open(root/(mode+'.log'),'a');logs.append(log)
                proc=subprocess.Popen([sys.executable,'-B','-c',CHILD,mode,str(financial),str(queue_path)],
                                      cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=log)
                procs.append(proc)
            def wait(predicate,seconds=50):
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    if any(p.poll() is not None for p in procs):
                        raise AssertionError('Worker exited: '+str({p.name:p.read_text(errors='replace')[-2500:] for p in root.glob('*.log')}))
                    if predicate():return
                    time.sleep(.2)
                raise AssertionError('Workers did not complete: '+str({p.name:p.read_text(errors='replace')[-1500:] for p in root.glob('*.log')}))
            def done(ident,market):
                row=q.db.execute('SELECT status FROM unified_jobs WHERE update_id=? AND market=?',(ident,market)).fetchone()
                return row and row[0]=='DONE'
            try:
                for mode in ('spot','futures','cross','margin'):start(mode)
                wait(lambda:all((root/name).exists() for name in ('runtime-health.json','futures-health.json','cross-health.json','margin-health.json')))
                ident=1
                for market,text in (('spot','/pair SOLUSDT'),('futures','/pair SOLUSDT'),
                                    ('futures_cross','/pair BTCUSDT SOLUSDT'),('margin_cross','/pair BTCUSDT SOLUSDT')):
                    q.receive(ident,True,'/mode '+market);ident+=1
                    with shared.transaction():snapshot=pair_snapshots(shared,requested_market=market)
                    q.receive(ident,True,text,pairs=snapshot);ident+=1
                    proposals=q.pair_proposals();proposal=next(x for x in proposals if x['market']==market and x['status']=='OPEN')
                    q.receive(ident,True,'/confirm '+proposal['id']);current=ident;ident+=1
                    wait(lambda:done(current,market))
                wait(lambda:(shared._get('spot','quote') or {}).get('symbol')=='SOLUSDT' and
                            (shared._get('futures','quote') or {}).get('symbol')=='SOLUSDT')
                self.assertTrue(shared.snapshot('spot')['paused']);self.assertTrue(shared.snapshot('futures')['paused'])
                self.assertTrue(all(r[0] for r in shared.db.execute("SELECT paused FROM wallet_state WHERE wallet IN ('futures_cross','margin_cross')")))
                self.assertTrue(shared.ledger.reconcile()['balanced'])
                first_spot=procs[0].pid
                wait(lambda:(root/'pollers.log').exists())
                self.assertEqual(set((root/'pollers.log').read_text().split()),{str(first_spot)})
                for p in procs:p.send_signal(signal.SIGTERM)
                for p in procs:p.wait(timeout=15)
                for log in logs:log.close()
                procs.clear();logs.clear()
                for name in ('runtime-health.json','futures-health.json','cross-health.json','margin-health.json'):
                    (root/name).unlink()
                for mode in ('spot','futures','cross','margin'):start(mode)
                wait(lambda:all((root/name).exists() for name in ('runtime-health.json','futures-health.json','cross-health.json','margin-health.json')))
                wait(lambda:(shared._get('spot','quote') or {}).get('symbol')=='SOLUSDT' and
                            (shared._get('futures','quote') or {}).get('symbol')=='SOLUSDT')
                self.assertTrue(shared.snapshot('spot')['paused']);self.assertTrue(shared.snapshot('futures')['paused'])
                self.assertTrue(all(r[0] for r in shared.db.execute("SELECT paused FROM wallet_state WHERE wallet IN ('futures_cross','margin_cross')")))
                self.assertTrue(shared.ledger.reconcile()['balanced'])
                wait(lambda:str(procs[0].pid) in (root/'pollers.log').read_text().split())
                self.assertEqual(set((root/'pollers.log').read_text().split()),{str(first_spot),str(procs[0].pid)})
            finally:
                for p in procs:
                    if p.poll() is None:p.send_signal(signal.SIGTERM)
                for p in procs:
                    try:p.wait(timeout=15)
                    except subprocess.TimeoutExpired:p.kill();p.wait()
                for log in logs:log.close()
                q.close();shared.close()
