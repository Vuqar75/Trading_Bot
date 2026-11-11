"""Build a legacy-executor-compatible public snapshot from one technical capture."""
import hashlib,json
from pathlib import Path
import numpy as np
from research.adaptive_shadow import canonical
from research.pattern_ai_analyze import analyze


def build(capture,bundle,now_ms):
    packet=analyze(bundle,capture,now_ms)
    rows=np.asarray(capture['candles'],dtype=float);n=len(rows);c=rows[:,4]
    h=c[:n//4*4].reshape(-1,4)[:,-1];four=c[:n//16*16].reshape(-1,16)[:,-1]
    # Capture starts at UTC4h boundary; these starts refer to completed groups.
    origin=int(rows[0,0]);last15=int(rows[-1,0]);last1=origin+(len(h)-1)*3600000;last4=origin+(len(four)-1)*14400000
    returns=h[-20:]/h[-21:-1]-1
    features={'close_4h':float(four[-1]),'sma20_4h':float(four[-20:].mean()),'change_4h_pct':float((four[-1]/four[-2]-1)*100),
              'volatility_20h_pct':float(returns.std()*100),'close_15m':float(c[-1]),'rsi14_15m':packet['features']['rsi']*100,
              'atr14_15m_pct':packet['features']['atr_pct']*100,'adx14_15m':packet['features']['adx']*100}
    s={'schema':1,'symbol':capture['symbol'],'venue':'binance-usdm','as_of_ms':now_ms,'received_ms':now_ms,
       'latest_4h_ms':last4,'latest_1h_ms':last1,'latest_15m_ms':last15,'features':features,
       'book':{'source_event_time':'unavailable','execution_quote':False},'news':[],
       'coverage':'Только закрытые USD-M свечи и технические признаки; новостей и исполнимой котировки нет.',
       'technical_context':packet}
    s['snapshot_id']=hashlib.sha256(canonical(s).encode()).hexdigest()
    base='TREND_UP' if four[-1]>four[-20:].mean()*1.01 else 'TREND_DOWN' if four[-1]<four[-20:].mean()*.99 else 'RANGE'
    return s,base
