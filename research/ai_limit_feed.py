"""Public USD-M inputs only, allowlisted HTTPS, no credentials/POST/retries."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from .ai_limit_plan import SYMBOLS,positive,dec,text,integer,fresh,digest,quote as check_quote,context

HOST='https://fapi.binance.com'
from .derivatives_context import PATHS as DERIVATIVE_PATHS
PATHS={'/fapi/v1/time','/fapi/v1/ticker/bookTicker','/fapi/v1/premiumIndex','/fapi/v1/aggTrades','/fapi/v1/exchangeInfo','/fapi/v1/fundingRate','/fapi/v1/klines'}|set(DERIVATIVE_PATHS)


class Feed:
    def __init__(self,*,fetch=None,clock=lambda:int(time.time()*1000)):
        self.clock=clock;self.fetch=fetch or self.get;self.denied=None;self.cached_filters=None
        self.trade_report={}
    def get(self,path,params):
        if self.denied:raise ValueError(self.denied)
        if path not in PATHS or set(params)-{'symbol','limit','fromId','startTime','endTime','interval','period'}:raise ValueError('public_endpoint_denied')
        if 'symbol' in params and params['symbol'] not in SYMBOLS:raise ValueError('symbol_denied')
        url=HOST+path+('?' +urllib.parse.urlencode(params) if params else '')
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'TradingBot-AI-Paper-Brackets/1.0'})
            with urllib.request.urlopen(request,timeout=8) as response:raw=response.read(3000001)
        except urllib.error.HTTPError as error:
            status=error.code;error.close()
            if status in (401,403,451):self.denied='public_access_denied_http'+str(status)
            raise ValueError(self.denied or 'public_market_http_failure') from None
        except (OSError,TimeoutError):raise ValueError('public_market_unavailable') from None
        if len(raw)>3000000:raise ValueError('public_response_size')
        try:return json.loads(raw)
        except ValueError:raise ValueError('public_response_json') from None
    def book(self,symbol):
        if symbol not in SYMBOLS:raise ValueError('symbol_denied')
        raw=self.fetch('/fapi/v1/ticker/bookTicker',{'symbol':symbol});mark=self.fetch('/fapi/v1/premiumIndex',{'symbol':symbol});now=self.clock()
        if raw.get('symbol')!=symbol or mark.get('symbol')!=symbol:raise ValueError('quote_symbol')
        event=min(integer(raw['time']),integer(mark['time']))
        value={'id':digest([raw,mark]),'liquidity_id':digest(raw),'symbol':symbol,'event_ms':event,'received_ms':now,
            'bid':raw['bidPrice'],'ask':raw['askPrice'],'bid_qty':raw['bidQty'],'ask_qty':raw['askQty'],'mark':mark['markPrice']}
        check_quote(value,symbol,now)
        due=integer(mark['nextFundingTime'])
        return value,due
    def filters(self,symbol):
        now=self.clock()
        if self.cached_filters is None or now-self.cached_filters[0]>=60000:
            self.cached_filters=(now,self.fetch('/fapi/v1/exchangeInfo',{}))
        matches=[s for s in self.cached_filters[1].get('symbols',[]) if s.get('symbol')==symbol]
        if len(matches)!=1 or matches[0].get('contractType')!='PERPETUAL' or matches[0].get('status')!='TRADING' or matches[0].get('marginAsset')!='USDT':raise ValueError('contract_not_trading')
        f={x['filterType']:x for x in matches[0]['filters']}
        result={'as_of_ms':self.cached_filters[0],'tick_size':f['PRICE_FILTER']['tickSize'],'step_size':f['LOT_SIZE']['stepSize'],
            'min_qty':f['LOT_SIZE']['minQty'],'min_notional':f['MIN_NOTIONAL'].get('notional',f['MIN_NOTIONAL'].get('minNotional'))}
        from .ai_limit_plan import filters
        filters(result,now)
        return result
    def trades(self,symbol,last_id=None):
        if symbol not in SYMBOLS:raise ValueError('symbol_denied')
        params={'symbol':symbol,'limit':1000}
        if last_id is not None:params['fromId']=last_id+1
        started=self.clock();raw=self.fetch('/fapi/v1/aggTrades',params);now=self.clock()
        if not isinstance(raw,list) or len(raw)>1000:raise ValueError('trade_list')
        events=[];prior=last_id;gap=False;discontinuities=[];stale=0;future=0;ages=[];stale_times=[]
        for r in raw:
            ident=integer(r['a']);stamp=integer(r['T'])
            if prior is not None and ident!=prior+1:
                gap=True
                if len(discontinuities)<8:discontinuities.append({'expected':prior+1,'received':ident})
            prior=ident
            if r.get('s',symbol)!=symbol or type(r.get('m')) is not bool:raise ValueError('trade_symbol_or_side')
            positive(r['p']);positive(r['q']);ages.append(now-stamp)
            if not 0<=now-stamp<=5000:
                stale+=int(now-stamp>5000);future+=int(stamp>now)
                if now-stamp>5000:stale_times.append(stamp)
                gap=True;continue
            item={'id':str(ident),'symbol':symbol,'event_ms':stamp,'received_ms':now,'price':r['p'],'qty':r['q'],'buyer_maker':r['m']}
            positive(item['price']);positive(item['qty']);fresh(item,now);events.append(item)
        self.trade_report[symbol]={'symbol':symbol,'requested_after_id':last_id,'started_ms':started,'received_ms':now,
            'fetch_ms':now-started,'rows':len(raw),'first_id':integer(raw[0]['a']) if raw else None,
            'last_id':prior,'oldest_age_ms':max(ages) if ages else None,'newest_age_ms':min(ages) if ages else None,
            'last_event_ms':integer(raw[-1]['T']) if raw else None,'stale_rows':stale,'future_rows':future,
            'stale_first_ms':min(stale_times) if stale_times else None,'stale_last_ms':max(stale_times) if stale_times else None,
            'discontinuities':discontinuities,'batch_hash':digest(raw),'gap':gap}
        return events,prior,gap
    def funding(self,start,end,*,symbols=SYMBOLS):
        # One continuous global coverage window is confirmed for ALL supported
        # symbols. The caller additionally checks previously observed due times.
        if not 0<=start<=end or end-start>7*86400000:raise ValueError('funding_recovery_window_requires_review')
        events=[]
        if any(s not in SYMBOLS for s in symbols):raise ValueError('funding_symbols')
        for symbol in symbols:
            rows=self.fetch('/fapi/v1/fundingRate',{'symbol':symbol,'startTime':start,'endTime':end,'limit':1000})
            if not isinstance(rows,list) or len(rows)>=1000:raise ValueError('funding_pagination_unconfirmed')
            seen=set()
            for r in rows:
                stamp=integer(r['fundingTime'])
                if r.get('symbol')!=symbol or stamp in seen or not start<=stamp<=end:raise ValueError('funding_history_shape')
                seen.add(stamp);positive(r['markPrice']);dec(r['fundingRate'])
                events.append({'symbol':symbol,'event_ms':stamp,'rate':r['fundingRate'],'mark':r['markPrice']})
        return sorted(events,key=lambda r:(r['event_ms'],r['symbol']))
    def capture(self,symbol):
        server=integer(self.fetch('/fapi/v1/time',{})['serverTime'])
        if abs(server-self.clock())>5000:raise ValueError('exchange_clock_untrusted')
        rows=self.fetch('/fapi/v1/klines',{'symbol':symbol,'interval':'15m','limit':1000})
        closed=[]
        for r in rows:
            if type(r[0]) is not int or r[6]!=r[0]+900000-1:raise ValueError('source_candle_time')
            if r[0]+900000<=server-60000:closed.append([r[0],*r[1:6]])
        if len(closed)<960:raise ValueError('source_warmup')
        return {'symbol':symbol,'venue':'binance-usdm','candles':closed[-960:]}
    def news(self):
        from .adaptive_shadow import headlines
        items,failed=headlines(clock=self.clock);result=[]
        for item in items[:3]:
            published=integer(item['published_ms'])
            result.append({'id':str(len(result)),'title':item['title'][:250],'url':item['url'][:500],
                'published_ms':published,'received_ms':item['observed_ms']})
        return {'coverage':'limited','high_risk_until_ms':0,'items':result}
    def snapshot(self,symbol,*,news=None):
        # Long candle/news fetches precede the final executable quote.
        capture=self.capture(symbol);f=self.filters(symbol);news=self.news() if news is None else news
        from .derivatives_context import collect
        signal=capture['candles'][-1][0]+900000
        capture['derivatives']=collect(self.fetch,symbol=symbol,signal_ms=signal,now_ms=self.clock(),clock=self.clock)
        book,_=self.book(symbol);value=context(capture,book,f,news,self.clock())
        return value,capture
