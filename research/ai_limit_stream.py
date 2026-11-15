"""Bounded public USD-M websocket transport. Network thread never touches ledger.
Reconnect/gap requires an idle bootstrap; missing execution is never backfilled.
"""
import json
import threading
import time
from collections import deque
from .ai_limit_feed import Feed
from .ai_limit_plan import integer,positive,SYMBOLS

class StreamFeed(Feed):
    streaming=True
    def __init__(self,*,symbols,connect=None,capacity=32768,batch=256,**kwargs):
        super().__init__(**kwargs)
        self.lock=threading.Lock();self.stop=threading.Event();self.capacity=capacity;self.batch=batch
        self.states={s:dict(queue=deque(),last=None,fault=True,reason='bootstrap',connected=False,highwater=0,duplicates=0,reconnects=0,received=0) for s in symbols}
        self.connect=connect;self.threads=[];self.timeout_errors=(TimeoutError,)
    def start(self):
        if self.connect is None:
            import websocket
            self.timeout_errors=(websocket.WebSocketTimeoutException,TimeoutError)
            self.connect=lambda url:websocket.create_connection(url,timeout=2,enable_multithread=True)
        for symbol in self.states:
            t=threading.Thread(target=self.run,args=(symbol,),daemon=True);t.start();self.threads.append(t)
        return self
    def fail(self,symbol,reason,category=None):
        with self.lock:
            st=self.states[symbol];st['fault']=True;st['reason']=reason;st['connected']=False
            st['last_fault']={'at_ms':self.clock(),'category':category or reason}
    def receive(self,symbol,raw):
        if raw.get('e')!='aggTrade' or raw.get('s')!=symbol or type(raw.get('m')) is not bool:raise ValueError('stream_shape')
        ident=integer(raw['a']);stamp=integer(raw['T']);positive(raw['p']);positive(raw['q'])
        item={'a':ident,'T':stamp,'p':raw['p'],'q':raw['q'],'m':raw['m'],'s':symbol}
        with self.lock:
            st=self.states[symbol]
            if st['last'] is not None and ident<=st['last']:
                st['duplicates']+=1
                # Identical ID replay cannot cause a second financial action.
                # Revised duplicate is unconfirmed, even if no longer buffered.
                if {k:v for k,v in st.get('last_item',{}).items() if not k.startswith('_')}!=item:st['fault']=True;st['reason']='duplicate_or_revision'
                return
            if st['last'] is not None and ident!=st['last']+1:st['fault']=True;st['reason']='id_gap'
            item['_received_ns']=time.monotonic_ns()
            st['last']=ident;st['last_item']=item;st['received']+=1
            if len(st['queue'])>=self.capacity:
                st['fault']=True;st['reason']='overflow';st['queue'].clear()
            st['queue'].append(item);st['highwater']=max(st['highwater'],len(st['queue']))
    def run(self,symbol):
        while not self.stop.is_set():
            ws=None
            try:
                ws=self.connect('wss://fstream.binance.com/market/ws/'+symbol.lower()+'@aggTrade')
                with self.lock:
                    st=self.states[symbol];st['connected']=True;st['reconnects']+=1
                while not self.stop.is_set():
                    try:raw=ws.recv()
                    except self.timeout_errors:
                        # A quiet socket/read deadline is not a disconnect or
                        # evidence of missing IDs. Freshness is checked separately.
                        with self.lock:
                            st=self.states[symbol];st['read_timeouts']=st.get('read_timeouts',0)+1
                        continue
                    if not raw:raise OSError('closed')
                    if len(raw)>65536:raise ValueError('stream_size')
                    self.receive(symbol,json.loads(raw))
            except Exception as error:self.fail(symbol,'disconnect_or_invalid',type(error).__name__)
            finally:
                if ws is not None:
                    try:ws.close()
                    except Exception:pass
            self.stop.wait(1)
    def trades(self,symbol,last_id=None):
        with self.lock:
            st=self.states[symbol];fault=st['fault'] or not st['connected']
            if last_id is None:
                # Only Poller's financially-idle resynchronization uses None.
                rows=([st['last_item']] if st.get('last_item') else []);st['queue'].clear()
                if rows and st['connected']:
                    st['fault']=False;st['reason']=None
                fault=True
            else:
                while st['queue'] and st['queue'][0]['a']<=last_id:st['queue'].popleft()
                rows=[st['queue'].popleft() for _ in range(min(self.batch,len(st['queue'])))]
            diag={k:st[k] for k in ('reason','connected','highwater','duplicates','reconnects','received')}
            diag['queued']=len(st['queue'])
            diag['read_timeouts']=st.get('read_timeouts',0);diag['last_fault']=st.get('last_fault')
            diag['oldest_dequeued_wait_ms']=(time.monotonic_ns()-rows[0]['_received_ns'])//1000000 if rows else None
        parser=Feed(fetch=lambda *_:rows,clock=self.clock)
        events,cursor,gap=parser.trades(symbol,last_id)
        self.trade_report[symbol]={**parser.trade_report[symbol],'transport':'websocket','stream':diag,'gap':bool(gap or fault)}
        return ([] if gap or fault else events),cursor,bool(gap or fault)
    def pending(self):
        with self.lock:return any(st["queue"] for st in self.states.values())
    def close(self):
        self.stop.set()
        for t in self.threads:t.join(timeout=3)
