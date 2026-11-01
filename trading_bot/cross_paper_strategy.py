"""Offline deterministic cross strategy on externally validated data.

No network, clock source, Telegram polling or process supervision is provided.
The caller is responsible for proven funding coverage and fail-closed recovery.
"""
import json
import time
from decimal import Decimal

from .core import signal_details
from .cross_paper_book import SYMBOLS
from .futures_data import INTERVALS
from .paper_wallet_ledger import amount


class CrossPaperStrategy:
    def __init__(self, book, *, fault=lambda point:None):
        self.book=book;self.shared=book.shared;self.db=book.db;self.fault=fault
        with self.shared.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS cross_signals(symbol TEXT PRIMARY KEY, candle INTEGER NOT NULL, payload TEXT NOT NULL)')

    @staticmethod
    def _signal(rows, interval, server_ms):
        if interval not in INTERVALS or type(server_ms) is not int or not isinstance(rows,list) or len(rows)<31:
            raise ValueError('Insufficient closed candles')
        period=INTERVALS[interval]*1000
        parsed=[]
        for row in rows:
            if not isinstance(row,(list,tuple)) or len(row)!=2 or type(row[0]) is not int or row[0]%period:
                raise ValueError('Invalid candle')
            parsed.append((row[0],amount(row[1],positive=True)))
        if any(b[0]-a[0]!=period for a,b in zip(parsed,parsed[1:])):
            raise ValueError('Discontinuous candles')
        if not 0 <= server_ms-(parsed[-1][0]+period) <= period:
            raise ValueError('Stale or future candle')
        info=signal_details([price for _,price in parsed])
        return parsed[-1][0],{k:str(v) if isinstance(v,Decimal) else v for k,v in info.items()}

    def tick(self, quotes, candles, server_ms, *, now=None):
        now=time.time() if now is None else now
        try:
            if type(server_ms) is not int or abs(server_ms-int(now*1000))>60000:
                raise ValueError('Exchange time inconsistent')
            self.book.record_quotes(quotes,now=now)
        except (ValueError,TypeError,KeyError):
            self.book.fail_closed();raise
        risk=self.book.risk(quotes,now=now);actions=[]
        if risk['positions'] and risk['buffer']<=0:
            p=risk['positions'][0]
            result=self.book.close('liquidate:'+p['id'],p['symbol'],p['id'],p['quantity'],quotes,now=now,liquidate=True)
            return [{'action':'LIQUIDATION','result':result}]
        for p in risk['positions']:
            mark=amount(quotes[p['symbol']]['mark'],positive=True)
            movement=(mark/amount(p['entry'],positive=True)-1)*(1 if p['direction']=='LONG' else -1)
            reason='STOP' if movement<=Decimal('-.02') else 'TAKE' if movement>=Decimal('.04') else None
            if reason:
                result=self.book.close('protect:'+p['id']+':'+reason,p['symbol'],p['id'],p['quantity'],quotes,now=now,reason=reason)
                actions.append({'action':reason,'symbol':p['symbol'],'result':result})
        settings=self.db.execute('SELECT leverage,interval FROM cross_settings WHERE id=1').fetchone()
        if not settings:raise ValueError('Missing cross settings')
        try:
            if not isinstance(candles,dict):raise ValueError('Missing candle snapshot')
            signals={s:self._signal(candles.get(s),settings['interval'],server_ms) for s in SYMBOLS}
        except (ValueError,TypeError,KeyError):
            self.book.set_paused(True);raise
        closed={a['symbol'] for a in actions if a['action'] in ('STOP','TAKE')}
        for symbol in SYMBOLS:
            stamp,signal=signals[symbol]
            prior=self.db.execute('SELECT candle FROM cross_signals WHERE symbol=?',(symbol,)).fetchone()
            if prior and stamp<=prior[0]:
                if stamp<prior[0]:raise ValueError('Candle time moved backwards')
                continue
            paused=self.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]
            occupied=self.db.execute('SELECT 1 FROM cross_positions WHERE symbol=?',(symbol,)).fetchone()
            action=signal['action']
            if not paused and not occupied and symbol not in closed and action in ('BUY','SELL'):
                try:
                    result=self.book.open('signal:'+symbol+':'+str(stamp),symbol,'LONG' if action=='BUY' else 'SHORT',
                                          '5',settings['leverage'],quotes,now=now,expected_interval=settings['interval'])
                    self.fault('after_open_commit')
                    actions.append({'action':'OPEN','symbol':symbol,'result':result})
                except ValueError as exc:
                    if str(exc) not in ('Insufficient cross collateral','Cross is paused','Cross symbol already occupied'):
                        raise
                    actions.append({'action':'SKIP','symbol':symbol,'reason':str(exc)})
            with self.shared.transaction():
                current=self.db.execute('SELECT candle FROM cross_signals WHERE symbol=?',(symbol,)).fetchone()
                if not current or current[0]<stamp:
                    self.db.execute('INSERT OR REPLACE INTO cross_signals VALUES(?,?,?)',(symbol,stamp,json.dumps(signal,sort_keys=True)))
        return actions
