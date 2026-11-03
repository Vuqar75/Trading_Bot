"""Offline Spot margin paper strategy; data is fetched by the caller.

No network or Telegram operations occur here. The candle decision and any
resulting paper trade share one SQLite transaction, including crash recovery.
"""
import json
from decimal import Decimal, localcontext

from .cross_paper_strategy import CrossPaperStrategy
from .futures_data import INTERVALS
from .margin_paper_book import FEE, PAIRS, SLIPPAGE
from .margin_spot_filters import budget_lot
from .paper_wallet_ledger import amount,encode


class MarginPaperStrategy:
    def __init__(self,book,*,fault=lambda point:None):
        self.book=book;self.ledger=book.ledger;self.db=book.db;self.fault=fault
        self.last_protection_ms=None
        with self.ledger.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_signals(symbol TEXT PRIMARY KEY,candle INTEGER NOT NULL,payload TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_exit_fences(symbol TEXT PRIMARY KEY,exit_ms INTEGER NOT NULL)')

    def _exit(self,position,quotes,now_ms,reason):
        with self.ledger.transaction():
            result=self.book.close('protect:'+position['id']+':'+reason,position['symbol'],position['id'],position['quantity'],quotes,now_ms=now_ms)
            self.db.execute('INSERT INTO margin_exit_fences VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET exit_ms=MAX(exit_ms,excluded.exit_ms)',(position['symbol'],now_ms))
            self.fault('after_exit_before_commit')
            return result

    def tick(self,quotes,candles,*,now_ms):
        if type(now_ms) is not int or now_ms<0:raise ValueError('Invalid strategy clock')
        try:
            for symbol in PAIRS:
                q=quotes.get(symbol) if isinstance(quotes,dict) else None
                if not isinstance(q,dict) or type(q.get('server_ms')) is not int or abs(q['server_ms']-now_ms)>60000:
                    raise ValueError('Spot exchange clock inconsistent')
            self.book.record_quotes(quotes,now_ms=now_ms)
            risk=self.book.risk(quotes,now_ms=now_ms);actions=[]
            if risk['positions'] and risk['equity']<=risk['maintenance']:
                positions=self.book._positions()
                with self.ledger.transaction():
                    result=self.book.liquidate('margin:liquidation:'+str(now_ms),quotes,now_ms=now_ms)
                    for p in positions:
                        self.db.execute('INSERT INTO margin_exit_fences VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET exit_ms=MAX(exit_ms,excluded.exit_ms)',(p['symbol'],now_ms))
                    self.fault('after_liquidation_before_commit')
                self.last_protection_ms=now_ms
                return [{'action':'LIQUIDATION','result':result}]
            for p in self.book._positions():
                mark=amount(quotes[p['symbol']]['price'],positive=True);entry=amount(p['entry'],positive=True)
                with localcontext() as ctx:
                    ctx.prec=120
                    movement=(mark/entry-1)*(1 if p['direction']=='LONG' else -1)
                reason='STOP' if movement<=Decimal('-.02') else 'TAKE' if movement>=Decimal('.04') else None
                if reason:
                    result=self._exit(p,quotes,now_ms,reason)
                    actions.append({'action':reason,'symbol':p['symbol'],'result':result})
            self.last_protection_ms=now_ms
            settings=self.db.execute('SELECT leverage,interval FROM margin_settings WHERE id=1').fetchone()
            if not settings or type(settings['leverage']) is not int or not 1<=settings['leverage']<=3 or settings['interval'] not in INTERVALS:
                raise ValueError('Invalid margin settings')
            if not isinstance(candles,dict):raise ValueError('Spot candles unavailable')
            for symbol in PAIRS:
                q=quotes[symbol]
                with localcontext() as ctx:
                    ctx.prec=120
                    stamp,signal=CrossPaperStrategy._signal(candles.get(symbol),settings['interval'],min(q['server_ms'],now_ms))
                with self.ledger.transaction():
                    prior=self.db.execute('SELECT candle FROM margin_signals WHERE symbol=?',(symbol,)).fetchone()
                    if prior and stamp<=prior['candle']:
                        if stamp<prior['candle']:raise ValueError('Spot candle moved backwards')
                        continue
                    fence=self.db.execute('SELECT exit_ms FROM margin_exit_fences WHERE symbol=?',(symbol,)).fetchone()
                    exited=bool(fence and stamp+INTERVALS[settings['interval']]*1000<=fence['exit_ms'])
                    paused=self.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]
                    occupied=self.db.execute('SELECT 1 FROM margin_positions WHERE symbol=?',(symbol,)).fetchone()
                    if not paused and not occupied and not exited and signal['action'] in ('BUY','SELL'):
                        mark=amount(q['price'],positive=True);direction='LONG' if signal['action']=='BUY' else 'SHORT'
                        filters=self.book._filters(quotes,symbol,now_ms)
                        with localcontext() as ctx:
                            ctx.prec=120
                            execution=mark*(1+SLIPPAGE if direction=='LONG' else 1-SLIPPAGE)
                        quantity=budget_lot(Decimal(5)*settings['leverage'],execution,FEE if direction=='LONG' else Decimal(0),mark,filters)
                        if not quantity:
                            actions.append({'action':'SKIP','symbol':symbol,'reason':'Spot lot or notional below model limit'})
                        else:
                            ident='margin:signal:'+symbol+':'+str(stamp)
                            try:
                                result=self.book.open(ident,ident,symbol,direction,encode(quantity),'5',settings['leverage'],quotes,now_ms=now_ms)
                                actions.append({'action':'OPEN','symbol':symbol,'result':result})
                            except ValueError as exc:
                                if str(exc) not in ('Insufficient free collateral','Margin is paused','Margin position limit'):
                                    raise
                                actions.append({'action':'SKIP','symbol':symbol,'reason':str(exc)})
                    self.fault('after_decision_before_commit')
                    self.db.execute('INSERT OR REPLACE INTO margin_signals VALUES(?,?,?)',(symbol,stamp,json.dumps(signal,sort_keys=True)))
                self.fault('after_decision_commit')
            return actions
        except (ValueError,TypeError,KeyError) as exc:
            self.book.fail_closed(type(exc).__name__)
            raise
