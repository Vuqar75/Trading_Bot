"""Offline margin-cross paper trades over the shared double-entry ledger.

This is not yet connected to market data, Telegram, transfers or a worker.
It must not be deployed. Prices and quantities are supplied by a test caller;
there are no network operations or order endpoints in this module.
"""
import hashlib
import json
from decimal import Decimal, localcontext

from .margin_interest_ledger import MarginInterestLedger
from .margin_spot_filters import affordable_lot, sellable_lot, validate_lot
from .paper_wallet_ledger import amount, encode
from .display_amount import display_amount

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
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_settings(id INTEGER PRIMARY KEY CHECK(id=1),leverage INTEGER NOT NULL,interval TEXT NOT NULL)')
            self.db.execute("INSERT OR IGNORE INTO margin_settings VALUES(1,2,'15m')")
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.db.execute("INSERT OR IGNORE INTO margin_runtime VALUES('error','null')")
            self.db.execute('CREATE TABLE IF NOT EXISTS margin_market_quotes(symbol TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            self.db.execute("INSERT OR IGNORE INTO wallet_state(wallet,paused) VALUES('margin_cross',1)")
            if not self.db.execute('SELECT 1 FROM margin_position_anchor WHERE id=1').fetchone():
                if self.db.execute('SELECT 1 FROM margin_positions LIMIT 1').fetchone():raise ValueError('Missing margin position anchor')
                self._anchor('initial')
            self._check()

    def _positions(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM margin_positions ORDER BY symbol')]

    def fail_closed(self,reason):
        if not isinstance(reason,str) or not reason or len(reason)>100:raise ValueError('Invalid margin error')
        with self.ledger.transaction():
            self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
            self.db.execute("INSERT OR REPLACE INTO margin_runtime VALUES('error',?)",(json.dumps(reason),))

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

    @staticmethod
    def _filters(quotes,symbol,now_ms):
        row=quotes.get(symbol) if isinstance(quotes,dict) else None
        filters=row.get('filters') if isinstance(row,dict) else None
        if (not isinstance(filters,dict) or type(filters.get('observed_ms')) is not int or
            not 0<=now_ms-filters['observed_ms']<=120_000):
            raise ValueError('Missing or stale Spot filters')
        if row.get('source')=='https://api.binance.com' and (not filters.get('notional_price') or
            type(filters.get('notional_time_ms')) is not int or not 0<=now_ms-filters['notional_time_ms']<=120_000):
            raise ValueError('Missing or stale Spot notional reference')
        return filters

    @staticmethod
    def _quote_identity(row):
        return {key:row.get(key) for key in ('price','time_ms','server_ms','source','price_kind','filters')}

    def record_quotes(self,quotes,*,now_ms,clock_ms=None):
        """Atomically publish both already-fetched prices; no network is done here."""
        self._marks(quotes,now_ms)
        with self.ledger.transaction():
            now_ms=clock_ms() if clock_ms else now_ms
            self._marks(quotes,now_ms)
            self._check()
            for symbol in PAIRS:
                row=self._quote_identity(quotes[symbol])
                if (type(row['server_ms']) is not int or not row['time_ms']<=row['server_ms'] or
                    abs(row['server_ms']-now_ms)>60000 or not isinstance(row['source'],str) or not row['source'] or
                    row['price_kind']!='last_public_trade'):
                    raise ValueError('Invalid Spot quote provenance')
                old=self.db.execute('SELECT payload FROM margin_market_quotes WHERE symbol=?',(symbol,)).fetchone()
                if old:
                    previous=json.loads(old['payload'])
                    if (row['time_ms']<previous['time_ms'] or row['server_ms']<previous['server_ms'] or
                        (row['time_ms']==previous['time_ms'] and amount(row['price'])!=amount(previous['price']))):
                        raise ValueError('Contradictory Spot quote')
                self.db.execute('INSERT OR REPLACE INTO margin_market_quotes VALUES(?,?)',(symbol,json.dumps(row,sort_keys=True)))

    def stored_quotes(self):
        return {row['symbol']:json.loads(row['payload']) for row in self.db.execute('SELECT symbol,payload FROM margin_market_quotes')}

    def _current_marks(self,quotes,now_ms):
        supplied=self._marks(quotes,now_ms);stored=self.stored_quotes();actual=self._marks(stored,now_ms)
        for symbol in PAIRS:
            if self._quote_identity(quotes[symbol])!=stored[symbol]:raise ValueError('Spot quote snapshot superseded')
        if supplied!=actual:raise ValueError('Spot quote snapshot mismatch')
        return actual

    def validate_transfer(self,quantity,*,outgoing,now_ms):
        """Called under the caller's financial transaction; never fetches data."""
        quantity=amount(quantity,positive=True)
        if type(outgoing) is not bool or type(now_ms) is not int:raise ValueError('Invalid margin transfer')
        self._check()
        row=self.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()
        if not row or row[0]!=1:raise ValueError('Margin wallet must be paused')
        if outgoing:
            error=self.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
            if not error or json.loads(error[0]) is not None:raise ValueError('Margin requires recovery')
            if self.db.execute("SELECT 1 FROM margin_loans l WHERE l.status='OPEN' AND NOT EXISTS (SELECT 1 FROM margin_positions p WHERE p.id=l.position_id)").fetchone():
                raise ValueError('Unattached margin liability')
            self._accrue_all(now_ms);self._check()
            marks=self._marks(self.stored_quotes(),now_ms);risk=self._risk(marks)
            if risk['available']<quantity or (risk['positions'] and risk['equity']-quantity<=risk['maintenance']):
                raise ValueError('Insufficient margin collateral')

    def _prior(self,operation,parts):
        if not isinstance(operation,str) or not operation:raise ValueError('Operation identity required')
        fp=hashlib.sha256(json.dumps(parts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        row=self.db.execute('SELECT fingerprint,result FROM margin_trade_operations WHERE id=?',(operation,)).fetchone()
        if row and row['fingerprint']!=fp:raise ValueError('Conflicting margin trade identity')
        return fp,json.loads(row['result']) if row else None

    def _remember(self,operation,fp,result):
        self.db.execute('INSERT INTO margin_trade_operations VALUES(?,?,?)',(operation,fp,json.dumps(result,sort_keys=True)))

    def _notify(self,text):
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='futures_outbox'").fetchone():
            self.db.execute('INSERT INTO futures_outbox(message) VALUES(?)',('[Spot margin cross • PAPER] '+text,))

    def _risk(self,marks):
        equity=Decimal(0)
        for asset,mark in marks.items():
            equity+=(self.ledger.balance(WALLET+':free',asset)+self.ledger.balance(WALLET+':reserved',asset)+self.ledger.balance(WALLET+':principal',asset)+self.ledger.balance(WALLET+':interest',asset))*mark
        positions=self.db.execute('SELECT * FROM margin_positions ORDER BY symbol').fetchall()
        locked=sum((amount(p['own']) for p in positions),Decimal(0))
        maintenance=sum((amount(p['quantity'])*marks[PAIRS[p['symbol']]]*MAINTENANCE for p in positions),Decimal(0))
        available=max(Decimal(0),min(self.ledger.balance(WALLET+':free'),equity-locked))
        return {'equity':equity,'locked':locked,'maintenance':maintenance,'available':available,'positions':len(positions)}

    def risk(self,quotes,*,now_ms,clock_ms=None):
        with self.ledger.transaction():
            now_ms=clock_ms() if clock_ms else now_ms
            self._check();marks=self._current_marks(quotes,now_ms)
            self._accrue_all(now_ms);self._check();return self._risk(marks)

    def _accrue_all(self,now_ms):
        for row in self.db.execute("SELECT id FROM margin_loans WHERE status='OPEN' ORDER BY id").fetchall():
            self.loans.accrue(row['id'],now_ms=now_ms)

    def _trade(self,event,symbol,side,quantity,mark,filters,now_ms):
        with localcontext() as ctx:
            ctx.prec=120
            quantity=validate_lot(quantity,mark,filters)
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

    def open(self,operation,position_id,symbol,direction,quantity,own,leverage,quotes,*,now_ms,clock_ms=None):
        quantity=amount(quantity,positive=True);own=amount(own,positive=True)
        if symbol not in PAIRS or direction not in ('LONG','SHORT') or not isinstance(position_id,str) or not position_id or own>5 or type(leverage) is not int or not 1<=leverage<=3:raise ValueError('Invalid margin entry')
        marks=self._marks(quotes,now_ms);mark=marks[PAIRS[symbol]]
        filters=self._filters(quotes,symbol,now_ms);validate_lot(quantity,mark,filters)
        parts=['open',position_id,symbol,direction,encode(quantity),encode(own),leverage,encode(mark),now_ms]
        with self.ledger.transaction(),localcontext() as ctx:
            ctx.prec=120
            fp,prior=self._prior(operation,parts)
            if prior is not None:return prior
            now_ms=clock_ms() if clock_ms else now_ms
            self._check()
            marks=self._current_marks(quotes,now_ms);mark=marks[PAIRS[symbol]]
            filters=self._filters(quotes,symbol,now_ms);validate_lot(quantity,mark,filters)
            self._accrue_all(now_ms)
            if self.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]:raise ValueError('Margin is paused')
            error=self.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()
            hold=self.db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()
            if not error or json.loads(error[0]) is not None or (hold and hold[0]=='true'):raise ValueError('Margin requires recovery')
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
            actual,fee=self._trade('margin:open:'+operation,symbol,'BUY' if direction=='LONG' else 'SELL',quantity,mark,filters,now_ms)
            self.db.execute('INSERT INTO margin_positions VALUES(?,?,?,?,?,?,?,?,?)',(position_id,symbol,direction,encode(quantity),encode(actual),encode(own),leverage,loan,now_ms))
            result={'position_id':position_id,'symbol':symbol,'direction':direction,'quantity':encode(quantity),'fee':encode(fee),'loan_id':loan}
            self._remember(operation,fp,result);self._anchor('open:'+operation);self._check()
            if self._risk(marks)['equity']<=self._risk(marks)['maintenance']:raise ValueError('Margin maintenance exhausted')
            self._notify('Открыта '+symbol+' '+direction+': '+display_amount(quantity,PAIRS[symbol])+' '+PAIRS[symbol]+'; комиссия '+display_amount(fee,'USDT')+' USDT.')
            return result

    def close(self,operation,symbol,position_id,expected_quantity,quotes,*,now_ms,expires_ms=None,clock_ms=None):
        return self._close(operation,symbol,position_id,expected_quantity,quotes,now_ms=now_ms,liquidation=False,expires_ms=expires_ms,clock_ms=clock_ms)

    def _close(self,operation,symbol,position_id,expected_quantity,quotes,*,now_ms,liquidation,expires_ms=None,clock_ms=None):
        expected_quantity=amount(expected_quantity,positive=True)
        if symbol not in PAIRS or type(liquidation) is not bool:raise ValueError('Invalid margin close')
        if expires_ms is not None and (type(expires_ms) is not int or not callable(clock_ms)):raise ValueError('Expiry requires a live clock')
        parts=['close',symbol,position_id,encode(expected_quantity),liquidation]
        with self.ledger.transaction(),localcontext() as ctx:
            ctx.prec=120
            fp,prior=self._prior(operation,parts)
            if prior is not None:return prior
            now_ms=clock_ms() if clock_ms else now_ms
            if type(now_ms) is not int or now_ms<0 or (expires_ms is not None and now_ms>=expires_ms):raise ValueError('Margin close confirmation expired')
            self._check();p=self.db.execute('SELECT * FROM margin_positions WHERE symbol=? AND id=?',(symbol,position_id)).fetchone()
            if not p or amount(p['quantity'])!=expected_quantity:raise ValueError('Position changed or missing')
            marks=self._current_marks(quotes,now_ms)
            filters=self._filters(quotes,symbol,now_ms)
            mark=marks[PAIRS[symbol]];loan_id=p['loan_id']
            if loan_id:self.loans.accrue(loan_id,now_ms=now_ms)
            loan=self.db.execute('SELECT * FROM margin_loans WHERE id=?',(loan_id,)).fetchone() if loan_id else None
            debt=amount(loan['principal'])+amount(loan['interest']) if loan and loan['status']=='OPEN' else Decimal(0)
            entry_results=[json.loads(row['result']) for row in self.db.execute('SELECT result FROM margin_trade_operations')]
            entries=[r for r in entry_results if r.get('position_id')==position_id and 'direction' in r and 'fee' in r]
            if len(entries)!=1:raise ValueError('Missing or ambiguous margin entry record')
            entry_fee=amount(entries[0]['fee']);exit_fee=Decimal(0);exit_price=mark*(1+SLIPPAGE if p['direction']=='SHORT' else 1-SLIPPAGE)
            interest_total=sum((amount(row['charge']) for row in self.db.execute('SELECT charge FROM margin_interest_periods WHERE loan_id=?',(loan_id,))),Decimal(0)) if loan_id else Decimal(0)
            if p['direction']=='LONG':
                qty=sellable_lot(p['quantity'],mark,filters) if liquidation else validate_lot(p['quantity'],mark,filters)
                if qty:exit_price,exit_fee=self._trade('margin:close:'+operation,symbol,'SELL',qty,mark,filters,now_ms)
            elif debt:
                already_owned=self.ledger.balance(WALLET+':free',PAIRS[symbol])
                needed=max(Decimal(0),debt-already_owned)
                qty=affordable_lot(needed,self.ledger.balance(WALLET+':free'),mark*(1+SLIPPAGE),FEE,mark,filters,allow_partial=liquidation)
                if qty>0:exit_price,exit_fee=self._trade('margin:close:'+operation,symbol,'BUY',qty,mark,filters,now_ms)
            paid=Decimal(0)
            if debt:
                asset='USDT' if p['direction']=='LONG' else PAIRS[symbol]
                paid=min(debt,self.ledger.balance(WALLET+':free',asset))
                if paid:self.loans.repay(operation+':repay',loan_id,encode(paid),now_ms=now_ms)
            remaining=debt-paid
            self.db.execute('DELETE FROM margin_positions WHERE id=?',(position_id,))
            if remaining:self.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
            result={'position_id':position_id,'debt_asset':('USDT' if p['direction']=='LONG' else PAIRS[symbol]),'repaid':encode(paid),'remaining_debt':encode(remaining),'liquidation':liquidation}
            # Crypto interest is valued at the close execution price for a
            # transparent paper estimate, never used to mutate the ledger.
            complete=not remaining and (p['direction']!='LONG' or qty==amount(p['quantity']))
            with localcontext() as context:
                context.prec=120
                gross=(exit_price-amount(p['entry']))*amount(p['quantity'])*(1 if p['direction']=='LONG' else -1)
                interest_value=interest_total*(exit_price if p['direction']=='SHORT' else Decimal(1))
                net=gross-entry_fee-exit_fee-interest_value if complete else None
            self.ledger._post('margin:position-close:'+operation,'FINANCIAL_METADATA',{
                'position_id':position_id,'symbol':symbol,'direction':p['direction'],'at_ms':now_ms,
                'gross':encode(gross) if complete else None,'entry_fee':encode(entry_fee),'exit_fee':encode(exit_fee),
                'interest':encode(interest_total),'interest_asset':('USDT' if p['direction']=='LONG' else PAIRS[symbol]),
                'interest_value':encode(interest_value) if complete else None,'net':encode(net) if net is not None else None,
                'interest_valuation':'close_execution_price' if p['direction']=='SHORT' else 'USDT',
                'complete':complete,'liquidation':liquidation},[],created=now_ms/1000)
            self._remember(operation,fp,result);self._anchor('close:'+operation);self._check()
            self._notify(('Модельная ликвидация ' if liquidation else 'Закрыта ')+symbol+' '+p['direction']+'; итог '+display_amount(net,'USDT')+' USDT; остаток долга '+display_amount(remaining,result['debt_asset'])+' '+result['debt_asset']+'.')
            return result

    def liquidate(self,operation,quotes,*,now_ms,clock_ms=None):
        marks=self._marks(quotes,now_ms)
        parts=['liquidate',[(s,encode(marks[a])) for s,a in PAIRS.items()],now_ms]
        with self.ledger.transaction():
            fp,prior=self._prior(operation,parts)
            if prior is not None:return prior
            now_ms=clock_ms() if clock_ms else now_ms
            self._check();marks=self._current_marks(quotes,now_ms);self._accrue_all(now_ms)
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
                filters=self._filters(quotes,symbol,now_ms)
                qty=sellable_lot(surplus,marks[asset],filters)
                if qty:self._trade('margin:liquidate-surplus:'+operation+':'+asset,symbol,'SELL',qty,marks[asset],filters,now_ms)
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
