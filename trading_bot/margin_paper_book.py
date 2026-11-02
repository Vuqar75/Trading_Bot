"""Offline margin-cross paper trades over the shared double-entry ledger.

This is not yet connected to market data, Telegram, transfers or a worker.
It must not be deployed. Prices and quantities are supplied by a test caller;
there are no network operations or order endpoints in this module.
"""
import hashlib
import json
from decimal import Decimal, localcontext, ROUND_DOWN

from .margin_interest_ledger import MarginInterestLedger
from .paper_wallet_ledger import amount, encode

WALLET='margin_cross'
PAIRS={'BTCUSDT':'BTC','ETHUSDT':'ETH'}
FEE=Decimal('0.001')
SLIPPAGE=Decimal('0.0002')
MAINTENANCE=Decimal('0.05')  # Explicit paper assumption; not Binance tiers.


class MarginPaperBook:
    def __init__(self,ledger):
        self.ledger=ledger;self.db=ledger.db
        self.loans=MarginInterestLedger(ledger)
        with ledger.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_positions(id TEXT PRIMARY KEY,symbol TEXT NOT NULL UNIQUE,direction TEXT NOT NULL,quantity TEXT NOT NULL,entry TEXT NOT NULL,own TEXT NOT NULL,leverage INTEGER NOT NULL,loan_id TEXT,opened_ms INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_trade_operations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_position_anchor(id INTEGER PRIMARY KEY CHECK(id=1),event_id TEXT NOT NULL REFERENCES ledger_events(id))')
            self.db.execute("INSERT OR IGNORE INTO wallet_state(wallet,paused) VALUES('margin_cross',1)")
            if not self.db.execute('SELECT 1 FROM margin_position_anchor WHERE id=1').fetchone():
                if self.db.execute('SELECT 1 FROM margin_positions LIMIT 1').fetchone():raise ValueError('Missing margin position anchor')
                self._anchor('initial')
            self._check()

    def _positions(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM margin_positions ORDER BY symbol')]

    def _anchor(self,operation):
        ident='margin:position-anchor:'+operation
        self.ledger._post(ident,'FINANCIAL_METADATA',{'positions':self._positions()},[])
        self.db.execute('INSERT OR REPLACE INTO margin_position_anchor VALUES(1,?)',(ident,))

    def _check(self):
        self.loans._check()
        anchor=self.db.execute('SELECT e.details FROM margin_position_anchor a JOIN ledger_events e ON e.id=a.event_id WHERE a.id=1').fetchone()
        if not anchor or json.loads(anchor['details']).get('positions')!=self._positions():raise ValueError('Margin position journal mismatch')
        for pos in self.db.execute('SELECT * FROM margin_positions'):
            if pos['symbol'] not in PAIRS or pos['direction'] not in ('LONG','SHORT') or amount(pos['quantity'])<=0 or amount(pos['entry'])<=0 or not 0<amount(pos['own'])<=5 or type(pos['leverage']) is not int or not 1<=pos['leverage']<=3:
                raise ValueError('Invalid margin position')
            if pos['loan_id']:
                loan=self.db.execute('SELECT position_id,asset FROM margin_loans WHERE id=?',(pos['loan_id'],)).fetchone()
                if not loan or loan['position_id']!=pos['id'] or loan['asset']!=('USDT' if pos['direction']=='LONG' else PAIRS[pos['symbol']]):
                    raise ValueError('Margin loan and position disagree')
            if pos['direction']=='LONG':
                asset=PAIRS[pos['symbol']]
                if self.ledger.balance(WALLET+':free',asset)+self.ledger.balance(WALLET+':reserved',asset)<amount(pos['quantity']):
                    raise ValueError('Margin position exceeds accounted asset')

    @staticmethod
    def _marks(quotes,now_ms):
        if type(now_ms) is not int or now_ms<0 or not isinstance(quotes,dict):raise ValueError('Invalid quote snapshot')
        marks={'USDT':Decimal(1)}
        for symbol,asset in PAIRS.items():
            q=quotes.get(symbol)
            if not isinstance(q,dict) or type(q.get('time_ms')) is not int or not 0<=now_ms-q['time_ms']<=120_000:
                raise ValueError('Missing or stale Spot quote')
            marks[asset]=amount(q.get('price'),positive=True)
        return marks

    def _prior(self,operation,parts):
        if not isinstance(operation,str) or not operation:raise ValueError('Operation identity required')
        fp=hashlib.sha256(json.dumps(parts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        row=self.db.execute('SELECT fingerprint,result FROM margin_trade_operations WHERE id=?',(operation,)).fetchone()
        if row and row['fingerprint']!=fp:raise ValueError('Conflicting margin trade identity')
        return fp,json.loads(row['result']) if row else None

    def _remember(self,operation,fp,result):
        self.db.execute('INSERT INTO margin_trade_operations VALUES(?,?,?)',(operation,fp,json.dumps(result,sort_keys=True)))

    def _risk(self,marks):
        equity=Decimal(0)
        for asset,mark in marks.items():
            equity+=(self.ledger.balance(WALLET+':free',asset)+self.ledger.balance(WALLET+':reserved',asset)+self.ledger.balance(WALLET+':principal',asset)+self.ledger.balance(WALLET+':interest',asset))*mark
        positions=self.db.execute('SELECT * FROM margin_positions ORDER BY symbol').fetchall()
        locked=sum((amount(p['own']) for p in positions),Decimal(0))
        maintenance=sum((amount(p['quantity'])*marks[PAIRS[p['symbol']]]*MAINTENANCE for p in positions),Decimal(0))
        available=max(Decimal(0),min(self.ledger.balance(WALLET+':free'),equity-locked))
        return {'equity':equity,'locked':locked,'maintenance':maintenance,'available':available,'positions':len(positions)}

    def risk(self,quotes,*,now_ms):
        with self.ledger.transaction():
            self._check();marks=self._marks(quotes,now_ms)
            self._accrue_all(now_ms);self._check();return self._risk(marks)

    def _accrue_all(self,now_ms):
        for row in self.db.execute("SELECT id FROM margin_loans WHERE status='OPEN' ORDER BY id").fetchall():
            self.loans.accrue(row['id'],now_ms=now_ms)

    def _trade(self,event,symbol,side,quantity,mark,now_ms):
        with localcontext() as ctx:
            ctx.prec=120
            base=PAIRS[symbol];price=mark*(1+SLIPPAGE if side=='BUY' else 1-SLIPPAGE)
            notional=quantity*price;fee=notional*FEE
            if side=='BUY':
                posts=[(WALLET+':free','USDT',-notional-fee),('external:margin_trade','USDT',notional),('external:margin_fee','USDT',fee),
                       (WALLET+':free',base,quantity),('external:margin_trade',base,-quantity)]
            else:
                posts=[(WALLET+':free','USDT',notional-fee),('external:margin_trade','USDT',-notional),('external:margin_fee','USDT',fee),
                       (WALLET+':free',base,-quantity),('external:margin_trade',base,quantity)]
            self.ledger._post(event,'MARGIN_TRADE',{'symbol':symbol,'side':side,'quantity':encode(quantity),'mark':encode(mark),'price':encode(price),'fee':encode(fee),'at_ms':now_ms},posts,created=now_ms/1000)
        if self.ledger.balance(WALLET+':free','USDT')<0 or self.ledger.balance(WALLET+':free',base)<0:raise ValueError('Insufficient margin asset')
        return price,fee

    def open(self,operation,position_id,symbol,direction,quantity,own,leverage,quotes,*,now_ms):
        quantity=amount(quantity,positive=True);own=amount(own,positive=True)
        if symbol not in PAIRS or direction not in ('LONG','SHORT') or not isinstance(position_id,str) or not position_id or own>5 or type(leverage) is not int or not 1<=leverage<=3:raise ValueError('Invalid margin entry')
        marks=self._marks(quotes,now_ms);mark=marks[PAIRS[symbol]]
        parts=['open',position_id,symbol,direction,encode(quantity),encode(own),leverage,encode(mark),now_ms]
        with self.ledger.transaction(),localcontext() as ctx:
            ctx.prec=120
            fp,prior=self._prior(operation,parts)
            if prior is not None:return prior
            self._check()
            self._accrue_all(now_ms)
            if self.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]:raise ValueError('Margin is paused')
            if self.db.execute('SELECT 1 FROM margin_positions WHERE symbol=?',(symbol,)).fetchone() or self.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0]>=2:raise ValueError('Margin position limit')
            if self.db.execute("SELECT 1 FROM margin_loans l WHERE l.status='OPEN' AND NOT EXISTS (SELECT 1 FROM margin_positions p WHERE p.id=l.position_id)").fetchone():raise ValueError('Unattached margin liability')
            risk=self._risk(marks)
            if risk['available']<own:raise ValueError('Insufficient free collateral')
            price=mark*(1+SLIPPAGE if direction=='LONG' else 1-SLIPPAGE)
            trade_value=quantity*price
            if trade_value*(1+FEE if direction=='LONG' else 1)>own*leverage:raise ValueError('Leverage budget exceeded')
            loan=position_id+':loan'
            borrowed=max(Decimal(0),trade_value*(1+FEE)-own) if direction=='LONG' else quantity
            if borrowed:
                self.loans.borrow(operation+':borrow',loan,position_id,'USDT' if direction=='LONG' else PAIRS[symbol],encode(borrowed),now_ms=now_ms)
            else:loan=None
            actual,fee=self._trade('margin:open:'+operation,symbol,'BUY' if direction=='LONG' else 'SELL',quantity,mark,now_ms)
            self.db.execute('INSERT INTO margin_positions VALUES(?,?,?,?,?,?,?,?,?)',(position_id,symbol,direction,encode(quantity),encode(actual),encode(own),leverage,loan,now_ms))
            result={'position_id':position_id,'symbol':symbol,'direction':direction,'quantity':encode(quantity),'fee':encode(fee),'loan_id':loan}
            self._remember(operation,fp,result);self._anchor('open:'+operation);self._check()
            if self._risk(marks)['equity']<=self._risk(marks)['maintenance']:raise ValueError('Margin maintenance exhausted')
            return result

    def close(self,operation,symbol,position_id,expected_quantity,quotes,*,now_ms):
        return self._close(operation,symbol,position_id,expected_quantity,quotes,now_ms=now_ms,liquidation=False)

    def _close(self,operation,symbol,position_id,expected_quantity,quotes,*,now_ms,liquidation):
        marks=self._marks(quotes,now_ms)
        expected_quantity=amount(expected_quantity,positive=True)
        if symbol not in PAIRS or type(liquidation) is not bool:raise ValueError('Invalid margin close')
        parts=['close',symbol,position_id,encode(expected_quantity),encode(marks[PAIRS[symbol]]),now_ms,liquidation]
        with self.ledger.transaction(),localcontext() as ctx:
            ctx.prec=40;ctx.rounding=ROUND_DOWN
            fp,prior=self._prior(operation,parts)
            if prior is not None:return prior
            self._check();p=self.db.execute('SELECT * FROM margin_positions WHERE symbol=? AND id=?',(symbol,position_id)).fetchone()
            if not p or amount(p['quantity'])!=expected_quantity:raise ValueError('Position changed or missing')
            mark=marks[PAIRS[symbol]];loan_id=p['loan_id']
            if loan_id:self.loans.accrue(loan_id,now_ms=now_ms)
            loan=self.db.execute('SELECT * FROM margin_loans WHERE id=?',(loan_id,)).fetchone() if loan_id else None
            debt=amount(loan['principal'])+amount(loan['interest']) if loan and loan['status']=='OPEN' else Decimal(0)
            if p['direction']=='LONG':
                self._trade('margin:close:'+operation,symbol,'SELL',amount(p['quantity']),mark,now_ms)
            elif debt:
                already_owned=self.ledger.balance(WALLET+':free',PAIRS[symbol])
                needed=max(Decimal(0),debt-already_owned)
                cost=needed*mark*(1+SLIPPAGE)*(1+FEE);free=self.ledger.balance(WALLET+':free')
                if cost>free and not liquidation:raise ValueError('Insufficient USDT to buy borrowed asset')
                qty=needed if cost<=free else free/(mark*(1+SLIPPAGE)*(1+FEE))
                if qty>0:self._trade('margin:close:'+operation,symbol,'BUY',qty,mark,now_ms)
            paid=Decimal(0)
            if debt:
                asset='USDT' if p['direction']=='LONG' else PAIRS[symbol]
                paid=min(debt,self.ledger.balance(WALLET+':free',asset))
                if paid:self.loans.repay(operation+':repay',loan_id,encode(paid),now_ms=now_ms)
            remaining=debt-paid
            self.db.execute('DELETE FROM margin_positions WHERE id=?',(position_id,))
            if remaining:self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
            result={'position_id':position_id,'debt_asset':('USDT' if p['direction']=='LONG' else PAIRS[symbol]),'repaid':encode(paid),'remaining_debt':encode(remaining),'liquidation':liquidation}
            self._remember(operation,fp,result);self._anchor('close:'+operation);self._check();return result

    def liquidate(self,operation,quotes,*,now_ms):
        marks=self._marks(quotes,now_ms)
        parts=['liquidate',[(s,encode(marks[a])) for s,a in PAIRS.items()],now_ms]
        with self.ledger.transaction():
            fp,prior=self._prior(operation,parts)
            if prior is not None:return prior
            self._check();self._accrue_all(now_ms)
            risk=self._risk(marks)
            if not risk['positions'] or risk['equity']>risk['maintenance']:
                raise ValueError('Margin maintenance has not been breached')
            self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
            positions=self.db.execute("SELECT * FROM margin_positions ORDER BY CASE direction WHEN 'LONG' THEN 0 ELSE 1 END,symbol").fetchall()
            results=[]
            for p in (p for p in positions if p['direction']=='LONG'):
                results.append(self._close(operation+':'+p['id'],p['symbol'],p['id'],p['quantity'],quotes,now_ms=now_ms,liquidation=True))
            # Sell only unreserved asset above all debt in that asset. Keep the
            # rest available for repayment, and never draw another wallet.
            for symbol,asset in PAIRS.items():
                debt=-(self.ledger.balance(WALLET+':principal',asset)+self.ledger.balance(WALLET+':interest',asset))
                surplus=max(Decimal(0),self.ledger.balance(WALLET+':free',asset)-debt)
                if surplus:self._trade('margin:liquidate-surplus:'+operation+':'+asset,symbol,'SELL',surplus,marks[asset],now_ms)
            self._repay_orphans(operation+':before-shorts',now_ms)
            for p in (p for p in positions if p['direction']=='SHORT'):
                results.append(self._close(operation+':'+p['id'],p['symbol'],p['id'],p['quantity'],quotes,now_ms=now_ms,liquidation=True))
            self._repay_orphans(operation+':after-shorts',now_ms)
            for result in results:
                row=self.db.execute("SELECT principal,interest FROM margin_loans WHERE position_id=?",(result['position_id'],)).fetchone()
                if row:result['remaining_debt']=encode(amount(row['principal'])+amount(row['interest']))
            result={'positions':results,'equity_after':encode(self._risk(marks)['equity'])}
            self._remember(operation,fp,result);self._check();return result

    def _repay_orphans(self,operation,now_ms):
        rows=self.db.execute("SELECT l.* FROM margin_loans l WHERE l.status='OPEN' AND NOT EXISTS (SELECT 1 FROM margin_positions p WHERE p.id=l.position_id) ORDER BY l.opened_ms,l.id").fetchall()
        for row in rows:
            debt=amount(row['principal'])+amount(row['interest'])
            payable=min(debt,self.ledger.balance(WALLET+':free',row['asset']))
            if payable:self.loans.repay(operation+':'+row['id'],row['id'],encode(payable),now_ms=now_ms)
