"""Bounded public USD-M websocket transport. Network thread never touches ledger.
Reconnect/gap requires an idle bootstrap; missing execution is never backfilled.
"""
from contextlib import nullcontext
import json
import threading
import time
from collections import deque
from .ai_limit_feed import Feed
from .ai_limit_plan import integer,positive,SYMBOLS,dec,digest,quote

class StreamFeed(Feed):
    streaming=True
    def __init__(self,*,symbols,connect=None,capacity=32768,batch=256,quotes=False,quote_capacity=4096,**kwargs):
        super().__init__(**kwargs)
        if type(quote_capacity) is not int or not 1<=quote_capacity<=4096:raise ValueError('quote_capacity')
        self.lock=threading.Lock();self.stop=threading.Event();self.capacity=capacity;self.batch=batch
        self.states={s:dict(queue=deque(),last=None,fault=True,reason='bootstrap',connected=False,highwater=0,duplicates=0,reconnects=0,received=0) for s in symbols}
        self.connect=connect;self.threads=[];self.timeout_errors=(TimeoutError,)
        self.websocket_quotes=quotes;self.quote_capacity=quote_capacity
        self.quote_states={s:dict(book=None,mark=None,book_last=None,mark_last=None,queue=deque(),
            channels={c:{'connected':False,'connections':0} for c in ('book','market')},
            fault=None,pending_fault=None,overflows=0,duplicates=0,highwater=0,received={'book':0,'mark':0}) for s in symbols}
    def span(self,name):
        probe=getattr(self,'probe',None)
        return probe.phase(name) if probe else nullcontext()
    def get(self,path,params):
        with self.span('http_total'):return super().get(path,params)
    def start(self):
        if self.connect is None:
            import websocket
            self.timeout_errors=(websocket.WebSocketTimeoutException,TimeoutError)
            # recv() strictly decodes TEXT as UTF-8 in C; avoid the redundant
            # pure-Python validation pass in websocket-client 1.8.0.
            # TLS verification and all protocol/source checks remain enabled.
            self.connect=lambda url:websocket.create_connection(url,timeout=2,enable_multithread=True,skip_utf8_validation=True)
        for symbol in self.states:
            t=threading.Thread(target=self.run,args=(symbol,),daemon=True);t.start();self.threads.append(t)
            if self.websocket_quotes:
                t=threading.Thread(target=self.run,args=(symbol,'book'),daemon=True);t.start();self.threads.append(t)
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
    def quote_connection(self,symbol,channel,connected,reason=None):
        with self.lock:
            q=self.quote_states[symbol];q['channels'][channel]['connected']=connected
            if connected:q['channels'][channel]['connections']+=1
            # Each routed connection must provide a new absolute component.
            # Stable book identity survives reconnect: it cannot refill liquidity.
            q['book' if channel=='book' else 'mark']=None
            if reason:
                q['pending_fault']=reason;q['last_fault']={'at_ms':self.clock(),'channel':channel,'category':reason}

    def receive_quote(self,symbol,raw,channel):
        original=raw;partial=raw.get('e')=='depthUpdate'
        if partial:
            if channel!='book':raise ValueError('partial_depth_channel')
            for side,descending in [('b',True),('a',False)]:
                levels=raw[side]
                if not isinstance(levels,list) or not 1<=len(levels)<=5:raise ValueError('partial_depth_levels')
                for level in levels:
                    if not isinstance(level,list) or len(level)!=2:raise ValueError('partial_depth_level')
                    positive(level[0]);positive(level[1])
                prices=[dec(v[0]) for v in levels]
                if any((x<=y if descending else x>=y) for x,y in zip(prices,prices[1:])):raise ValueError('partial_depth_order')
            first=integer(raw['U']);last=integer(raw['u']);integer(raw['pu'])
            if first>last:raise ValueError('partial_depth_ids')
            raw={**raw,'e':'bookTicker','b':raw['b'][0][0],'B':raw['b'][0][1],'a':raw['a'][0][0],'A':raw['a'][0][1]}
        kind='book' if raw.get('e')=='bookTicker' else 'mark' if raw.get('e')=='markPriceUpdate' else None
        if (kind is None or raw.get('s')!=symbol or type(raw.get('st',1)) is not int or raw.get('st',1)!=1
                or (kind=='book')!=(channel=='book')):raise ValueError('quote_stream_identity')
        event=integer(raw['E']);now=self.clock()
        if kind=='book':
            stamp=integer(raw['T']);ident=integer(raw['u'])
            for k in ('b','a'):positive(raw[k])
            if dec(raw['b'])>dec(raw['a']) or any(dec(raw[k])<0 for k in ('B','A')):raise ValueError('quote_stream_book')
            item={k:raw[k] for k in ('e','s','u','E','T','b','B','a','A')}
        else:
            stamp=event;ident=event;positive(raw['p']);integer(raw['T'])
            item={k:raw[k] for k in ('e','s','E','p','T')}
        with self.lock:
            self.quote_states[symbol]['received'][kind]+=1
            if not all(0<=now-t<=5000 for t in (stamp,event)):
                # Invalid time must not poison the high-water identity and
                # suppress subsequent genuine frames until a reconnect.
                q=self.quote_states[symbol];q[kind]=None;q['pending_fault']=kind+'_source_time_unconfirmed'
                q['last_fault']={'at_ms':now,'kind':kind,'source_ms':stamp,'event_ms':event,
                                 'category':q['pending_fault']}
                return
            q=self.quote_states[symbol];old=q[kind+'_last'];old_id=old['u' if kind=='book' else 'E'] if old else None
            if ident<=q.get(kind+'_revision_id',-1):return
            if old is not None and ident<old_id:q['duplicates']+=1;return
            if old is not None and ident==old_id:
                if old!=item:
                    q[kind]=None;q[kind+'_revision_id']=ident;q['pending_fault']=kind+'_revision';return
                q['duplicates']+=1
                if q[kind] is not None:return  # no receipt-time rejuvenation
            if old is not None and (event<old['E'] or stamp<old.get('T',stamp) and kind=='book'):
                q[kind]=None;q['pending_fault']=kind+'_time_regression';return
            if partial and old is not None and ident>old_id and original['pu']!=old_id:
                q['pending_fault']='partial_depth_sequence_gap'
                q['last_fault']={'at_ms':now,'category':'partial_depth_sequence_gap','expected_pu':old_id,'observed_pu':original['pu']}
            q[kind+'_last']=item
            value={'kind':kind,'source':item,'source_ms':stamp,'event_ms':event,'received_ms':now,
                   'received_mono_ns':time.monotonic_ns()}
            if partial:value.update(source_kind='partial_depth5_100ms',raw=original)
            if len(q['queue'])>=self.quote_capacity:
                # The event history is incomplete, but latest absolute BBO
                # and mark snapshots remain independent evidence. Latch one
                # unconfirmed cycle; never manufacture historical fills.
                q['queue'].clear()
                q['pending_fault']='quote_queue_overflow';q['overflows']+=1
            q[kind]=value;q['queue'].append(value);q['highwater']=max(q['highwater'],len(q['queue']))

    def quote_packet(self,symbol):
        """No I/O: latest absolute BBO + mark, plus bounded protective events."""
        with self.lock:
            # Clock and components must describe the same locked snapshot.
            # A receiver can publish after an earlier clock read while this
            # thread waits for the lock; that is not a future receipt.
            now=self.clock()
            st=self.quote_states[symbol];book=st['book'];mark=st['mark'];updates=list(st['queue']);st['queue'].clear()
            channels={k:dict(v) for k,v in st['channels'].items()};fault=st['pending_fault'];st['pending_fault']=None
            diag={'transport':'websocket','channels':channels,'overflows':st['overflows'],
                  'duplicates':st['duplicates'],'highwater':st['highwater'],'last_fault':st.get('last_fault'),
                    'received':dict(st['received']),'book_stream':'depth5@100ms',
                  'received_ms':now,'components':{k:v for k,v in [('book',book),('mark',mark)] if v is not None}}
        reasons=[]
        for name,value in [('book',book),('mark',mark)]:
            if not channels['book' if name=='book' else 'market']['connected']:reasons.append(name+'_disconnected')
            if value is None:reasons.append(name+'_missing');continue
            for field in ('source_ms','event_ms'):
                age=now-value[field]
                if age<0:reasons.append(name+'_'+field+'_future')
                elif age>5000:reasons.append(name+'_'+field+'_stale')
            if not max(value['source_ms'],value['event_ms'])<=value['received_ms']<=now:reasons.append(name+'_receipt_time')
        if fault:reasons.append(fault)
        diag['time_rejections']=reasons;self.quote_report[symbol]=diag
        packet={'quote':None,'due':None,'report':diag,'updates':updates,'error':'public_quote_stream_unconfirmed' if reasons else None}
        if reasons:return packet
        b=book['source'];m=mark['source']
        value={'id':digest(['usdm-ws',b,m]),'liquidity_id':digest(['usdm-ws-book',b['s'],b['u'],b['T'],b['b'],b['B'],b['a'],b['A']]),
               'symbol':symbol,'event_ms':min(book['source_ms'],book['event_ms'],mark['source_ms']),
               'received_ms':max(book['received_ms'],mark['received_ms']),
               'bid':b['b'],'ask':b['a'],'bid_qty':b['B'],'ask_qty':b['A'],'mark':m['p']}
        if book.get('source_kind')=='partial_depth5_100ms':
            # A partial snapshot u can advance due to deeper levels alone.
            value['liquidity_id']='ws-partial-depth:'+digest([symbol,b['b'],b['B'],b['a'],b['A']])
        quote(value,symbol,now);packet.update(quote=value,due=m['T']);return packet

    def run(self,symbol,channel='market'):
        while not self.stop.is_set():
            ws=None
            try:
                suffix=symbol.lower()
                url=('wss://fstream.binance.com/public/ws/'+suffix+'@depth5@100ms' if channel=='book' else
                     'wss://fstream.binance.com/market/stream?streams='+suffix+'@aggTrade/'+suffix+'@markPrice@1s' if self.websocket_quotes else
                     'wss://fstream.binance.com/market/ws/'+suffix+'@aggTrade')
                ws=self.connect(url)
                if self.websocket_quotes:self.quote_connection(symbol,channel,True)
                if channel=='market':
                    with self.lock:
                        st=self.states[symbol];st['connected']=True;st['reconnects']+=1
                while not self.stop.is_set():
                    try:
                        with self.span('ws_recv_'+channel):raw=ws.recv()
                    except self.timeout_errors:
                        # A quiet socket/read deadline is not a disconnect or
                        # evidence of missing IDs. Freshness is checked separately.
                        with self.lock:
                            st=self.states[symbol] if channel=='market' else self.quote_states[symbol]['channels']['book']
                            st['read_timeouts']=st.get('read_timeouts',0)+1
                        continue
                    if not raw:raise OSError('closed')
                    if len(raw)>65536:raise ValueError('stream_size')
                    with self.span('ws_parse_'+channel):
                        if isinstance(raw,bytes):raw=raw.decode('utf-8')
                        data=json.loads(raw)
                    if self.websocket_quotes:
                        if channel=='market':
                            expected={symbol.lower()+'@aggTrade',symbol.lower()+'@markPrice@1s'}
                            if data.get('stream') not in expected:raise ValueError('quote_stream_envelope')
                            data=data['data']
                        if data.get('e')!='aggTrade':
                            with self.span('ws_dispatch_'+channel):self.receive_quote(symbol,data,channel)
                            continue
                    with self.span('ws_dispatch_'+channel):self.receive(symbol,data)
            except Exception as error:
                if channel=='market':self.fail(symbol,'disconnect_or_invalid',type(error).__name__)
                if self.websocket_quotes:self.quote_connection(symbol,channel,False,type(error).__name__)
            finally:
                if ws is not None:
                    try:ws.close()
                    except Exception:pass
            self.stop.wait(1)
    def trades(self,symbol,last_id=None):
        with self.span('trade_dequeue_parse'):return self._trades(symbol,last_id)
    def _trades(self,symbol,last_id=None):
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
        self.trade_report[symbol]={**parser.trade_report[symbol],'transport':'websocket','stream':diag,'stream_fault':bool(fault),'gap':bool(gap or fault)}
        return ([] if gap or fault else events),cursor,bool(gap or fault)
    def pending(self):
        # BBO updates are frequent even without actionable trade backlog.
        # They must not force financial/snapshot/fsync work at 50 Hz. Quotes
        # retain priority in every ordinary (<=100ms requested sleep) cycle.
        with self.lock:
            if self.websocket_quotes:return any(len(st['queue'])>=self.batch for st in self.states.values())
            return any(st["queue"] for st in self.states.values())
    def close(self):
        self.stop.set()
        for t in self.threads:t.join(timeout=3)
