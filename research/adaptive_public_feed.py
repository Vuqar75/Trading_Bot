"""Allowlisted Binance public market data for research. No keys or orders."""
import json,time,urllib.parse,urllib.request
from .adaptive_market_proof import quote
from .adaptive_funding_evidence import validate_mark
from .adaptive_shadow import candles

SPOT='https://data-api.binance.vision'
FUTURES='https://fapi.binance.com'
PATHS={(SPOT,'/api/v3/aggTrades'),(SPOT,'/api/v3/klines'),(SPOT,'/api/v3/exchangeInfo'),(SPOT,'/api/v3/time'),
       (FUTURES,'/fapi/v1/premiumIndex'),(FUTURES,'/fapi/v1/aggTrades'),(FUTURES,'/fapi/v1/fundingRate'),(FUTURES,'/fapi/v1/exchangeInfo'),(FUTURES,'/fapi/v1/time'),(FUTURES,'/fapi/v1/klines')}

def get(host,path,params):
 if (host,path) not in PATHS or not isinstance(params,dict) or (path not in ('/api/v3/time','/fapi/v1/time') and params.get('symbol')!='BTCUSDT') or (path in ('/api/v3/time','/fapi/v1/time') and params):raise ValueError('public_endpoint_denied')
 if any(k not in {'symbol','limit','interval','startTime','endTime'} for k in params):raise ValueError('public_parameter_denied')
 url=host+path+'?'+urllib.parse.urlencode(params)
 try:
  request=urllib.request.Request(url,headers={'User-Agent':'TradingBot-Adaptive-Research/1.0'})
  with urllib.request.urlopen(request,timeout=8) as response:raw=response.read(5000001)
  if len(raw)>5000000:raise ValueError('public_response_too_large')
  return json.loads(raw)
 except Exception:raise ValueError('public_source_unavailable') from None

def spot_quote(*,fetch=get,clock=lambda:int(time.time()*1000)):
 rows=fetch(SPOT,'/api/v3/aggTrades',{'symbol':'BTCUSDT','limit':1});received=clock()
 if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):raise ValueError('public_trade_invalid')
 return quote({'symbol':'BTCUSDT','source':'binance_spot_trade','price':rows[0].get('p'),'event_ms':rows[0].get('T'),'received_ms':received},market='spot',now_ms=received)

def futures_quote(*,fetch=get,clock=lambda:int(time.time()*1000)):
 raw=fetch(FUTURES,'/fapi/v1/premiumIndex',{'symbol':'BTCUSDT'});received=clock()
 result=validate_mark(raw,received_ms=received)
 return {**quote({'symbol':'BTCUSDT','source':'binance_usdm_mark','price':result['mark'],'event_ms':result['event_ms'],'received_ms':received},market='futures',now_ms=received),'next_due_ms':result['next_due_ms']}

def futures_execution_trade(*,not_before_ms,fetch=get,clock=lambda:int(time.time()*1000)):
 from .adaptive_market_proof import futures_trade_quote
 rows=fetch(FUTURES,'/fapi/v1/aggTrades',{'symbol':'BTCUSDT','limit':1});received=clock()
 if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):raise ValueError('public_trade_invalid')
 return futures_trade_quote({'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':rows[0].get('a'),'price':rows[0].get('p'),'event_ms':rows[0].get('T'),'received_ms':received},now_ms=received,not_before_ms=not_before_ms)

def funding_history(start_ms,end_ms,*,fetch=get,clock=lambda:int(time.time()*1000)):
 if type(start_ms) is not int or type(end_ms) is not int or not 0<=start_ms<=end_ms or end_ms-start_ms>7*86400000:raise ValueError('funding_window_invalid')
 rows=fetch(FUTURES,'/fapi/v1/fundingRate',{'symbol':'BTCUSDT','startTime':start_ms,'endTime':end_ms,'limit':1000});received=clock()
 if not isinstance(rows,list) or len(rows)>=1000 or received<end_ms:raise ValueError('funding_history_incomplete')
 return {'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':start_ms,'end_ms':end_ms,'received_ms':received,'rows':rows}

def filters(market,*,fetch=get,clock=lambda:int(time.time()*1000)):
 if market not in ('spot','futures'):raise ValueError('market_invalid')
 host,path=(SPOT,'/api/v3/exchangeInfo') if market=='spot' else (FUTURES,'/fapi/v1/exchangeInfo')
 data=fetch(host,path,{'symbol':'BTCUSDT'});received=clock()
 if not isinstance(data,dict) or not isinstance(data.get('symbols'),list):raise ValueError('filters_unavailable')
 matches=[s for s in data['symbols'] if isinstance(s,dict) and s.get('symbol')=='BTCUSDT']
 if len(matches)!=1 or not isinstance(matches[0].get('filters'),list):raise ValueError('filters_unavailable')
 if market=='futures' and (matches[0].get('contractType')!='PERPETUAL' or matches[0].get('marginAsset')!='USDT'):raise ValueError('contract_type_invalid')
 return {'market':market,'symbol':'BTCUSDT','received_ms':received,'filters':matches[0]}

def closed_15m(market,*,fetch=get,clock=lambda:int(time.time()*1000)):
 if market not in ('spot','futures'):raise ValueError('market_invalid')
 host,path,prefix=(SPOT,'/api/v3/klines','/api/v3/time') if market=='spot' else (FUTURES,'/fapi/v1/klines','/fapi/v1/time')
 server=fetch(host,prefix,{}).get('serverTime');begin=clock()
 if type(server) is not int or abs(server-begin)>5000:raise ValueError('exchange_clock_untrusted')
 raw=fetch(host,path,{'symbol':'BTCUSDT','interval':'15m','limit':200});received=clock()
 data=candles(raw,server,15,197)
 end=fetch(host,prefix,{}).get('serverTime');finished=clock()
 if type(end) is not int or abs(end-finished)>5000 or end<server or end//900000!=server//900000 or not 0<=finished-begin<=30000:raise ValueError('candle_boundary_or_latency_invalid')
 closed=[r for r in raw if type(r[6]) is int and r[6]<server]
 rows=[(r[0],float(r[1]),float(r[2]),float(r[3]),float(r[4])) for r in closed]
 if len(rows)!=len(data):raise ValueError('candle_capture_mismatch')
 return {'market':market,'symbol':'BTCUSDT','source':'binance_spot_klines' if market=='spot' else 'binance_usdm_klines','server_ms':server,'received_ms':received,'bars':rows[:-1],'skip_bar':rows[-1],'raw_bars':closed}

def execution_candles(market,warm,price,*,fetch=get,clock=lambda:int(time.time()*1000)):
 """Complete a pre-boundary history using one post-boundary candle fetch."""
 if market not in ('spot','futures') or not isinstance(warm,dict) or warm.get('market')!=market or warm.get('symbol')!='BTCUSDT':raise ValueError('warm_history_invalid')
 source='binance_spot_klines' if market=='spot' else 'binance_usdm_klines'
 if warm.get('source')!=source:raise ValueError('warm_history_source_invalid')
 now=clock();q=quote(price,market=market,now_ms=now);boundary=q['event_ms']//900000*900000
 if not 0<=q['event_ms']-boundary<=5000:raise ValueError('execution_window_missed')
 if type(warm.get('received_ms')) is not int or not 0<boundary-warm['received_ms']<=120000:raise ValueError('warm_history_stale')
 bars=warm.get('bars');last=warm.get('skip_bar')
 if not isinstance(bars,list) or not bars or not isinstance(last,(list,tuple)) or last[0]!=boundary-1800000:raise ValueError('warm_history_gap')
 host,path=(SPOT,'/api/v3/klines') if market=='spot' else (FUTURES,'/fapi/v1/klines')
 raw=fetch(host,path,{'symbol':'BTCUSDT','interval':'15m','limit':3});received=clock()
 data=candles(raw,q['event_ms'],15,1)
 if data[-1]['start_ms']!=boundary-900000:raise ValueError('execution_candle_gap')
 matches=[r for r in raw if r[0]==boundary-900000]
 if len(matches)!=1:raise ValueError('execution_candle_invalid')
 r=matches[0];skip=(r[0],float(r[1]),float(r[2]),float(r[3]),float(r[4]))
 result={'market':market,'symbol':'BTCUSDT','source':source,'received_ms':received,'warm_received_ms':warm['received_ms'],'bars':bars+[last],'skip_bar':skip}
 if isinstance(warm.get('raw_bars'),list):result['raw_bars']=warm['raw_bars']+[r]
 return result
