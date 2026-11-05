"""Paper-only daily Spot strategy, using the existing paper ledger and queue."""
import time
from decimal import Decimal
from .app import Application
from .spot_donchian import DAY_MS,WINDOW_MS,closed_days,decide,validate_execution
from .testnet_observer import ObservationError,number

class DonchianSpotApplication(Application):
    def __init__(self,*args,clock=time.time,**kwargs):
        self.clock=clock
        super().__init__(*args,**kwargs)
        if self.exchange.mode!='paper' or self.symbol!='BTCUSDT':raise ObservationError('Donchian requires BTCUSDT paper')
        with self.store.transaction():
            state=self.store.get('donchian60')
            if state is None:
                if number(self.store.get('position')['quantity']) or self.store.pending():raise ObservationError('Donchian requires cash and no pending orders')
                start=int(self.clock()*1000);state={'start_ms':start,'next_boundary_ms':(start//DAY_MS+1)*DAY_MS,'phase':'waiting','level':None,'last_action':None,'halted':False}
                self.store.set('donchian60',state)
            elif state.get('phase') in ('decided','executing'):
                state['halted']=True;state['error']='interrupted_decision_requires_review';self.store.set('donchian60',state);self.store.set('paused',True)

    def manual_close(self):
        pos=self.store.get('position');qty=number(pos['quantity']);request=self.store.get('close_request')
        if not request or not qty or self.store.pending() or self.store.get('blocked'):return False
        if not self.store.manual_request_valid(request,pos):
            with self.store.transaction():self.store.set('close_request',None);self.store.notify('[Spot paper] Подтверждение закрытия устарело.')
            return False
        q=self.reader.get('/api/v3/ticker/price',{'symbol':self.symbol});at=self.clock();price=number(q['price'])
        if q.get('symbol')!=self.symbol or not price:raise ObservationError('Manual close quote unavailable')
        info=self.reader.get('/api/v3/exchangeInfo',{'symbol':self.symbol})['symbols'][0]
        quantity=self._quantity(info,qty,price);ok=self.execute('SELL',quantity,price,'manual:'+str(request['update_id']),'MANUAL',pos['id'],quote_at=at)
        if ok:
            with self.store.transaction():
                state=self.store.get('donchian60')
                if not number(self.store.get('position')['quantity']):state['level']=None
                self.store.set('donchian60',state);self.store.set('close_request',None)
        return ok

    def tick(self):
        if self.store.get('identity').get('symbol','BTCUSDT')!=self.symbol or self.store.get('pair_selection_version',0)!=self.pair_version:raise ObservationError('Spot pair version superseded')
        self.recover()
        if self.manual_close():return
        quote=self.reader.get('/api/v3/ticker/price',{'symbol':self.symbol});at=self.clock()
        if quote.get('symbol')!=self.symbol or not number(quote['price']):raise ObservationError('Spot mark unavailable')
        state=self.store.get('donchian60')
        with self.store.transaction():
            self.store.set('quote',{'price':str(number(quote['price'])),'time':at})
            self.store.set('error',{'stage':'donchian','type':'ProtocolHalted','time':at,'reason':state.get('error','unconfirmed')} if state.get('halted') else None)
        now=int(self.clock()*1000);boundary=state['next_boundary_ms']
        if state.get('halted') or self.store.get('blocked') or self.store.pending():
            self.last_protection_at=None;self.last_protection_position=None;return
        if state.get('warmup_ms') is None:
            server=int(self.reader.get('/api/v3/time')['serverTime']);raw=self.reader.get('/api/v3/klines',{'symbol':self.symbol,'interval':'1d','limit':90})
            if abs(server-int(self.clock()*1000))>5000:raise ObservationError('Warmup clock mismatch')
            days=closed_days(raw,server)
            with self.store.transaction():
                state=self.store.get('donchian60');state['warmup_ms']=int(self.clock()*1000);state['warmup_last_day_ms']=days[-1][0];self.store.set('donchian60',state)
        current=self.store.get('position')
        if number(current['quantity']):self.last_protection_at=at;self.last_protection_position=current['id']
        if now<boundary:return
        if now>boundary+WINDOW_MS:
            with self.store.transaction():
                state=self.store.get('donchian60');state.update(halted=True,error='missed_daily_window');self.store.set('donchian60',state);self.store.set('paused',True);self.store.set('error',{'stage':'donchian','type':'ProtocolHalted','time':self.clock(),'reason':'missed_daily_window'})
            self.last_protection_at=None;self.last_protection_position=None
            return
        server=int(self.reader.get('/api/v3/time')['serverTime']);rows=self.reader.get('/api/v3/klines',{'symbol':self.symbol,'interval':'1d','limit':90})
        if abs(server-int(self.clock()*1000))>5000 or server<boundary:raise ObservationError('Daily data clock mismatch')
        days=closed_days(rows,server);pos=self.store.get('position');opened=bool(number(pos['quantity']))
        action,level=decide([x[1] for x in days],position_open=opened,prior_level=state['level'])
        with self.store.transaction():
            state=self.store.get('donchian60')
            if state['next_boundary_ms']!=boundary or state.get('halted') or int(self.clock()*1000)>boundary+WINDOW_MS:raise ObservationError('Daily decision stale after database wait')
            state.update(phase='decided',boundary_ms=boundary,signal_ms=int(self.clock()*1000),last_candle_ms=days[-1][0],last_action=action,proposed_level=str(level) if level is not None else None)
            self.store.set('donchian60',state)
        committed=int(self.clock()*1000)
        with self.store.transaction():
            state=self.store.get('donchian60');state['commit_ms']=committed
            if action=='HOLD' or (action=='BUY' and self.store.get('paused')):
                state.update(phase='waiting',next_boundary_ms=boundary+DAY_MS,level=str(level) if opened else None)
                if action=='BUY':state['last_result']='entry_skipped_paused'
            else:state['phase']='executing'
            self.store.set('donchian60',state)
        if state['phase']!='executing':return
        try:self._execute_decision(state,pos)
        except Exception:
            with self.store.transaction():
                current=self.store.get('donchian60');current.update(halted=True,error='execution_unconfirmed');self.store.set('donchian60',current);self.store.set('paused',True)
            raise

    def _execute_decision(self,state,pos):
        b=state['boundary_ms'];start=int(self.clock()*1000);before=int(self.reader.get('/api/v3/time')['serverTime']);trades=self.reader.get('/api/v3/trades',{'symbol':self.symbol,'limit':1});after=int(self.reader.get('/api/v3/time')['serverTime']);received=int(self.clock()*1000)
        if not isinstance(trades,list) or len(trades)!=1 or type(trades[0].get('id')) is not int:raise ObservationError('Trade quote ambiguous')
        trade=trades[0];price=number(trade['price']);validate_execution(boundary_ms=b,signal_commit_ms=state['commit_ms'],request_ms=start,trade_ms=trade['time'],received_ms=received,server_before_ms=before,server_after_ms=after)
        if trade['time']>after:raise ObservationError('Trade quote in future')
        with self.store.transaction():
            current=self.store.get('donchian60');check=int(self.clock()*1000)
            if current.get('phase')!='executing' or current.get('boundary_ms')!=b or not 0<=check-trade['time']<=5000 or check>b+WINDOW_MS:raise ObservationError('Trade quote expired after database wait')
            current['quote_trade_ms']=trade['time'];current['quote_observed_ms']=received;current['quote_request_ms']=start;self.store.set('donchian60',current)
        side=state['last_action'];price=price*(Decimal('1.0005') if side=='BUY' else Decimal('.9995'))
        info=self.reader.get('/api/v3/exchangeInfo',{'symbol':self.symbol})['symbols'][0]
        try:quantity=self._quantity(info,self.amount/price if side=='BUY' else number(pos['quantity']),price)
        except ObservationError as exc:
            if side!='BUY' or str(exc) not in ('Notional below exchange minimum','Quantity outside exchange filters','Untradeable residual (dust)'):raise
            with self.store.transaction():
                current=self.store.get('donchian60');current.update(phase='waiting',next_boundary_ms=b+DAY_MS,level=None,last_result='entry_skipped_exchange_filter');self.store.set('donchian60',current)
            return
        ok=self.execute(side,quantity,price,'donchian60:'+str(b),'DONCHIAN60_ENTRY' if side=='BUY' else 'DONCHIAN60_EXIT',pos['id'] if side=='SELL' else None,quote_at=received/1000)
        if not ok:raise ObservationError('Donchian execution not confirmed')
        with self.store.transaction():
            current=self.store.get('donchian60');remaining=number(self.store.get('position')['quantity'])
            if side=='SELL' and remaining:
                current.update(phase='waiting',next_boundary_ms=b+DAY_MS,level=state['proposed_level'],last_result='partial_residual',halted=True,error='unclosed_residual_requires_review');self.store.set('paused',True)
            else:current.update(phase='waiting',next_boundary_ms=b+DAY_MS,level=state['proposed_level'] if side=='BUY' else None,last_result='filled')
            self.store.set('donchian60',current)
