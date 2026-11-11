"""Read-only trained AI analysis from supplied closed USD-M candles.

No network/API key/budget/database access; stdout contains public technicals.
"""
import argparse,gzip,hashlib,json
from pathlib import Path
import numpy as np
from research import technical_patterns as tech


def analyze(bundle,capture,now_ms):
    if capture.get('venue')!='binance-usdm' or capture.get('symbol') not in ('BTCUSDT','ETHUSDT','SOLUSDT'):raise ValueError('candle_market')
    rows=capture['candles'];bar=15*60000
    if len(rows)<3000:raise ValueError('technical_warmup_insufficient')
    if rows[0][0]%(4*3600000):raise ValueError('technical_aggregate_alignment')
    for i,row in enumerate(rows):
        if len(row)!=6 or type(row[0]) is not int or row[0]%bar or row[0]+bar>now_ms:raise ValueError('candle_time')
        o,h,l,c,v=map(float,row[1:])
        if not np.isfinite([o,h,l,c,v]).all() or not 0<l<=min(o,c)<=max(o,c)<=h or v<0:raise ValueError('candle_values')
        if i and row[0]-rows[i-1][0]!=bar:raise ValueError('candle_gap')
    signal_ms=rows[-1][0]+bar
    if not signal_ms<=now_ms<signal_ms+bar:raise ValueError('analysis_stale')
    # Repeat each aggregate solely to reuse the causal15m feature constructor.
    # First minute open / last minute close are preserved; high/low not new evidence.
    bars=np.asarray([x[1:5] for x in rows],dtype=float)
    minute=np.repeat(bars,15,axis=0);volume=np.repeat(np.asarray([x[5] for x in rows],dtype=float)/15,15)
    X,flags,atr=tech.frame(minute,volume,rows[0][0]);last=X[-1];f=flags[-1]
    patterns=[name for name,yes in zip(tech.PATTERNS,f) if yes]
    long=bool(f[::2].any());short=bool(f[1::2].any());side=1 if long and not short else -1 if short and not long else 0
    if side:
        x=tech.vector(last,side,capture['symbol']);ml=tech.infer(bundle,x,symbol=capture['symbol'],signal_ms=signal_ms,now_ms=now_ms)
    else:ml={'action':'NO_TRADE','trade_allowed':False,'reason':'no_unambiguous_detected_pattern','model_id':bundle['model_id'],'symbol':capture['symbol'],'signal_ms':signal_ms}
    packet={'schema':1,'feature_version':tech.VERSION,'symbol':capture['symbol'],'venue':'binance-usdm','as_of_ms':now_ms,'signal_ms':signal_ms,
            'expires_ms':signal_ms+bar,'timeframe':'15m','price_kind':'last_closed_candle_not_executable_quote','close':rows[-1][4],
            'side':'LONG' if side==1 else 'SHORT' if side==-1 else 'WAIT','detected_patterns':patterns,
            'features':dict(zip(tech.FEATURES[:30],map(float,last))),'ml':ml,
            'limits':{'research_only':True,'orders_enabled':False,'probability_calibration':'not_established','future_profit':'not_established'}}
    if capture.get('derivatives') is not None:
        from research.derivatives_context import validate
        context=validate(capture['derivatives'],symbol=capture['symbol'],signal_ms=signal_ms,as_of_ms=now_ms)
        packet['derivatives']=context
    from research.technical_trade_scenarios import build
    packet['trade_scenarios']=build(packet)
    packet['packet_id']=hashlib.sha256(json.dumps(packet,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return packet


def main():
    p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--candles',required=True);p.add_argument('--now-ms',required=True,type=int);p.add_argument('--llm-request',action='store_true');args=p.parse_args()
    bundle=json.loads(Path(args.model).read_text());path=Path(args.candles)
    capture=json.load(gzip.open(path,'rt')) if path.suffix=='.gz' else json.loads(path.read_text())
    packet=analyze(bundle,capture,args.now_ms)
    if args.llm_request:
        from research.pattern_ai_snapshot import build
        from research.adaptive_ai_executor import request
        packet=request(build(capture,bundle,args.now_ms)[0])
    print(json.dumps(packet,ensure_ascii=False,indent=2,allow_nan=False))

if __name__=='__main__':main()
