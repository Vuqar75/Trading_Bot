"""Bounded public USD-M candle publication timing, no trades or secrets."""
import argparse,hashlib,json,os,time
from pathlib import Path
from .adaptive_public_feed import get,FUTURES
PERIOD=900000
OFFSETS=(2000,12000,30000,60000)

def read_candle(open_ms,*,fetch=get,clock=lambda:int(time.time()*1000)):
 if type(open_ms) is not int or open_ms%PERIOD:raise ValueError('invalid_open')
 before=clock();server=fetch(FUTURES,'/fapi/v1/time',{}).get('serverTime');after_server=clock()
 raw=fetch(FUTURES,'/fapi/v1/klines',{'symbol':'BTCUSDT','interval':'15m','startTime':open_ms,'endTime':open_ms+PERIOD-1,'limit':1});received=clock()
 if type(server) is not int or abs(server-after_server)>5000 or received<after_server or after_server<before:raise ValueError('clock_untrusted')
 if not isinstance(raw,list) or len(raw)!=1 or not isinstance(raw[0],list) or len(raw[0])<7 or raw[0][0]!=open_ms or raw[0][6]!=open_ms+PERIOD-1:raise ValueError('candle_identity_invalid')
 encoded=json.dumps(raw[0],separators=(',',':'),allow_nan=False).encode()
 return {'endpoint':'/fapi/v1/klines','symbol':'BTCUSDT','interval':'15m','price_type':'trade','open_ms':open_ms,'close_ms':raw[0][6],'server_ms':server,'server_received_ms':after_server,'request_ms':before,'received_ms':received,'closed_by_server_time':raw[0][6]<server,'exchange_closed_flag':'not_provided_by_rest','sha256':hashlib.sha256(encoded).hexdigest(),'raw':raw[0]}

def run(path,*,count=4,clock=lambda:int(time.time()*1000),sleep=time.sleep,fetch=get):
 if count!=4:raise ValueError('fixed_boundaries_required')
 fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);first=(clock()//PERIOD+1)*PERIOD
 with os.fdopen(fd,'w',encoding='utf8') as f:
  def emit(v):
   line=json.dumps(v,sort_keys=True,separators=(',',':'))+'\n'
   if f.tell()+len(line.encode())>2*1024*1024:raise ValueError('size_limit')
   f.write(line);f.flush();os.fsync(f.fileno())
  for i in range(count):
   boundary=first+i*PERIOD
   for offset in OFFSETS:
    target=boundary+offset;wait=target-clock()
    if wait>0:sleep(wait/1000)
    t=clock();entry={'boundary_ms':boundary,'offset_ms':offset,'observed_ms':t}
    if t-target>5000 or t<target:entry.update(state='missed',reason='capture_window')
    else:
     try:entry.update(state='received',candle=read_candle(boundary-PERIOD,fetch=fetch,clock=clock))
     except Exception as e:entry.update(state='unavailable',reason=type(e).__name__)
    emit(entry)
   if i:
    try:emit({'boundary_ms':boundary,'state':'previous_recheck','candle':read_candle(boundary-2*PERIOD,fetch=fetch,clock=clock)})
    except Exception as e:emit({'boundary_ms':boundary,'state':'previous_recheck_unavailable','reason':type(e).__name__})
 return {'first_boundary_ms':first,'boundaries':count}

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();print(json.dumps(run(a.output)))
if __name__=='__main__':main()
