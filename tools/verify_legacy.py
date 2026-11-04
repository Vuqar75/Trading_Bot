"""Compare the replay baseline against the unmodified original Git revision.

Read-only Git source access; both engines use in-memory SQLite and a fake exchange.
"""
import argparse
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.replay import ReplayExchange, load_candles, run_replay

BASELINE = '443ca56bacaf225c52ecef1a504a38e25672b8fc'


def verify(candles,start,end):
    code=subprocess.check_output(['git','show',BASELINE+':trading_bot/core.py'],text=True,encoding='utf-8')
    old=types.ModuleType('legacy_core')
    sys.modules[old.__name__]=old
    exec(compile(code,'legacy_core.py','exec'),old.__dict__)
    store=old.Store(':memory:')
    exchange=ReplayExchange()
    clock=[start/1000]
    try:
        with patch.object(old.time,'time',side_effect=lambda:clock[0]),patch.object(old,'ai_allows',return_value=True):
            engine=old.Engine(old.Config(),store,exchange)
            engine.change_market('futures');engine.change_leverage(1);store.set('paused',False)
            for i,candle in enumerate(candles):
                if not start<=candle.open_time<end:continue
                clock[0]=candle.open_time/1000
                exchange.advance(candle,candles[max(0,i-32):i])
                engine.tick()
            boundary=next(c for c in candles if c.open_time==end)
            clock[0]=end/1000
            if store.get('position'):engine.close_position(boundary.open,store.get('candle'),'Replay period end')
        original=store.db.execute('SELECT ts,direction,event,quantity,price,pnl,reason FROM futures_trades ORDER BY rowid').fetchall()
        fresh=run_replay(candles,start,end,capture=True)
        assert len(original)==len(fresh['journal']),(len(original),len(fresh['journal']))
        max_error=0.0
        for left,right in zip(original,fresh['journal']):
            values=[right[k] for k in ('ts','direction','event','quantity','fill_price','net_pnl','reason')]
            for a,b in zip(left,values):
                if isinstance(a,(float,int)):
                    max_error=max(max_error,abs(a-b));assert abs(a-b)<1e-9,(a,b)
                else:assert a==b,(a,b)
        return dict(revision=BASELINE,start_ms=start,end_ms=end,events=len(original),max_numeric_error=max_error,status='matched')
    finally:store.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--candles',type=Path,required=True)
    parser.add_argument('--split-ms',type=int,required=True)
    args=parser.parse_args()
    candles=load_candles(args.candles)
    print(json.dumps([verify(candles,candles[31].open_time,args.split_ms-60000),
                      verify(candles,args.split_ms,candles[-1].open_time)],indent=2))
