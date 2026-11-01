"""Offline single-file atomic accounting adapter.

The callback interface is for deterministic SQLite work only. It is not wired to
either bot and must not be used with real or Testnet orders. Trade integrations
must preserve their own immutable fill/order IDs and send no network request while
this transaction is held.
"""
import hashlib
import json
import sqlite3
import uuid
import time
from decimal import Decimal, localcontext
from pathlib import Path
from .paper_wallet_ledger import PaperWalletLedger, amount, encode


class SharedPaperStore:
    def __init__(self, path, *, fault=lambda p:None, timeout=5):
        self.db=sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=timeout)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON');self.db.execute('PRAGMA synchronous=FULL')
        self.fault=fault
        try:
            if self.db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':raise ValueError('Shared journal requires rollback mode')
            if self.db.execute("SELECT value FROM ledger_meta WHERE key='consolidated_schema'").fetchone()[0] != '1':raise ValueError('Unexpected shared schema')
            self.ledger=object.__new__(PaperWalletLedger);self.ledger.db=self.db;self.ledger.fault=lambda _:None
            with self.transaction():
                if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
                self._assert_consistent('spot');self._assert_consistent('futures')
                self.db.execute('CREATE TABLE IF NOT EXISTS shared_operations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL)')
        except BaseException:
            self.db.close();raise

    def close(self):self.db.close()

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def tx():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.fault('before_commit');self.db.commit();self.fault('after_commit')
            except BaseException:
                self.db.rollback();raise
        return tx()

    def _get(self,market,key,default=None):
        table='runtime_meta' if market=='spot' else 'futures_meta'
        row=self.db.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self,market,key,value):
        table='runtime_meta' if market=='spot' else 'futures_meta'
        self.db.execute('INSERT OR REPLACE INTO '+table+' VALUES(?,?)',(key,json.dumps(value,sort_keys=True)))

    def deployment_hold(self):
        row=self.db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()
        return bool(row and row[0]=='true')

    def _validate_hold_change(self, enabled):
        if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
        self._assert_consistent('spot');self._assert_consistent('futures')
        if not enabled and (self._get('spot','paused') is not True or self._get('futures','paused') is not True):
            raise ValueError('Both markets must be paused before release')
        if not enabled:
            for market in ('spot','futures'):
                quote=self._get(market,'quote') or {}
                if self._get(market,'error') or self._get(market,'blocked') or not quote.get('time') or not 0<=time.time()-quote['time']<=120:
                    raise ValueError('Market health must be checked before release')
            if not self._get('spot','position')['fees_known'] or self.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
                raise ValueError('Spot requires recovery or fee review')

    def set_deployment_hold(self,enabled):
        if type(enabled) is not bool:raise ValueError('Invalid hold state')
        with self.transaction():
            self._validate_hold_change(enabled)
            self.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold',?)",('true' if enabled else 'false',))

    def snapshot(self,market):
        if market not in ('spot','futures'):raise ValueError('Unknown market')
        pos=self._get(market,'position')
        if market=='spot':
            if not isinstance(pos,dict) or not isinstance(pos.get('fees_known'),bool):raise ValueError('Unknown Spot position')
            return {'cash':encode(amount(self._get(market,'cash'))),'position':pos,'pnl':self._get(market,'pnl'),'paused':self._get(market,'paused')}
        if pos is not None and not isinstance(pos,dict):raise ValueError('Unknown futures position')
        return {'cash':encode(amount(self._get(market,'cash'))),'position':pos,'pnl':self._get(market,'pnl'),'paused':self._get(market,'paused')}

    def _expected(self,market,snapshot):
        pos=snapshot['position']; cash=amount(snapshot['cash'])
        if cash<0:raise ValueError('Negative cash')
        if market=='spot':
            quantity=amount(pos['quantity'])
            if quantity<0:raise ValueError('Negative position')
            reserved,reserved_btc=self._spot_order_reserves()
            if cash<reserved or quantity<reserved_btc:raise ValueError('Reservation exceeds assets')
            return {('spot:free','USDT'):cash-reserved,('spot:reserved','USDT'):reserved,
                    ('spot:position','BTC'):quantity-reserved_btc,('spot:reserved','BTC'):reserved_btc}
        reserved=self.ledger.balance('futures_isolated:reserved','USDT')
        if cash<reserved:raise ValueError('Reservation exceeds assets')
        return {('futures_isolated:free','USDT'):cash-reserved,('futures_isolated:reserved','USDT'):reserved,
                ('futures_isolated:margin','USDT'):amount(pos['margin']) if pos else Decimal(0),
                ('futures_isolated:funding','USDT'):amount(pos['funding']) if pos else Decimal(0),
                ('futures_isolated:entry_fee','USDT'):-amount(pos['entry_fee']) if pos else Decimal(0)}

    def _spot_order_reserves(self):
        """Conservative reservation for unfinished paper Spot orders."""
        usdt=Decimal(0);btc=Decimal(0)
        for order in self.db.execute("SELECT client_id,side,quantity,price FROM runtime_orders WHERE phase!='SETTLED'"):
            fills=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM runtime_fills WHERE client_id=?',(order['client_id'],))]
            if order['side']=='BUY':
                spent=sum((amount(f['quoteQty'])+(amount(f['commission']) if f['commissionAsset']=='USDT' else 0) for f in fills),Decimal(0))
                usdt += max(Decimal(0),amount(order['quantity'])*amount(order['price'])*Decimal('1.002')-spent)
            elif order['side']=='SELL':
                filled=sum((amount(f['qty']) for f in fills),Decimal(0))
                btc += max(Decimal(0),amount(order['quantity'])-filled)
            else:raise ValueError('Unknown pending order side')
        return usdt,btc

    def _record_projection(self,market,before,after,event_id,extra=None):
        with localcontext() as c:
            c.prec=120
            postings=[]
            for (account,asset),value in self._expected(market,after).items():
                delta=value-self.ledger.balance(account,asset)
                if delta:postings += [(account,asset,delta),('external:'+market,asset,-delta)]
            if market=='spot':
                for fill in (extra or {}).get('new_fills',[]):
                    asset=fill['commissionAsset']; fee=amount(fill['commission'])
                    if asset not in ('BTC','USDT') and fee:
                        postings += [('spot:unvalued_fee',asset,-fee),('external:spot_fee',asset,fee)]
            previous=self._financial_projection(before);current=self._financial_projection(after)
            if postings or previous!=current:
                kind='FINANCIAL_CHANGE' if postings else 'FINANCIAL_METADATA'
                self.ledger._post(event_id,kind,{'market':market,'before':previous,'after':current,'details':extra or {}},postings)
                self.db.execute('UPDATE shared_market_anchor SET event_id=? WHERE market=?',(event_id,market))

    @staticmethod
    def _financial_projection(snapshot):
        pos=snapshot['position']
        if isinstance(pos,dict):pos={k:v for k,v in pos.items() if k!='funding_cursor'}
        return {'cash':snapshot['cash'],'position':pos,'pnl':snapshot['pnl']}

    def market_transaction(self,market):
        """Internal only: adapt an existing store transaction to the shared DB."""
        from contextlib import contextmanager
        @contextmanager
        def tx():
            with self.transaction():
                if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
                self._assert_consistent('spot');self._assert_consistent('futures')
                before=self.snapshot(market)
                if market=='spot':
                    seen=self.db.execute('SELECT COALESCE(MAX(rowid),0) FROM runtime_fills').fetchone()[0]
                else:
                    seen=self.db.execute('SELECT COALESCE(MAX(id),0) FROM futures_events').fetchone()[0]
                    seen_funding=self.db.execute('SELECT COALESCE(MAX(rowid),0) FROM futures_funding').fetchone()[0]
                yield
                if market=='spot':
                    new=[json.loads(r['payload']) for r in self.db.execute('SELECT payload FROM runtime_fills WHERE rowid>? ORDER BY rowid',(seen,))]
                    details={'new_fills':new,'fees_known':self._get('spot','position')['fees_known']}
                else:
                    new=[{'event':r['event'],'payload':json.loads(r['payload'])} for r in self.db.execute('SELECT event,payload FROM futures_events WHERE id>? ORDER BY id',(seen,))]
                    funding=[{'position_id':r['position_id'],'ts':r['ts'],'payload':json.loads(r['payload'])} for r in self.db.execute('SELECT position_id,ts,payload FROM futures_funding WHERE rowid>? ORDER BY rowid',(seen_funding,))]
                    details={'new_events':new,'new_funding':funding}
                self._record_projection(market,before,self.snapshot(market),'shared:tx:'+uuid.uuid4().hex,details)
                self._assert_consistent('spot');self._assert_consistent('futures')
                if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
        return tx()

    def _assert_consistent(self,market):
        snapshot=self.snapshot(market)
        with localcontext() as c:
            c.prec=120
            for (account,asset),expected in self._expected(market,snapshot).items():
                if self.ledger.balance(account,asset)!=expected:raise ValueError('Journal/state mismatch: '+account+' '+asset)
        row=self.db.execute('SELECT e.details FROM shared_market_anchor a JOIN ledger_events e ON e.id=a.event_id WHERE a.market=?',(market,)).fetchone()
        if not row or json.loads(row[0]).get('after')!=self._financial_projection(snapshot):
            raise ValueError('Journal/state mismatch: '+market+' financial projection')
        return snapshot

    def _sync_pause_and_validate(self,source,destination,quantity=None,now=None):
        if self.deployment_hold():raise ValueError('Deployment hold is active')
        if source not in ('reserve','spot','futures_isolated','futures_cross') or destination not in ('reserve','spot','futures_isolated','futures_cross') or source==destination:
            raise ValueError('Unsupported transfer')
        for wallet,market in (('spot','spot'),('futures_isolated','futures')):
            paused=self._get(market,'paused')
            if type(paused) is not bool:raise ValueError('Unknown pause state')
            self.db.execute('UPDATE wallet_state SET paused=? WHERE wallet=?',(int(paused),wallet))
            if wallet in (source,destination) and not paused:raise ValueError('Trading wallet must be paused')
        if 'spot' in (source,destination):
            spot=self.snapshot('spot')
            if not spot['position']['fees_known'] or self._get('spot','blocked') or self._get('spot','error'):
                raise ValueError('Spot requires recovery or fee valuation')
            if self.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():
                raise ValueError('Spot order unresolved')
        if 'futures_isolated' in (source,destination) and (self._get('futures','blocked') or self._get('futures','error')):
            raise ValueError('Futures requires recovery')
        if 'futures_cross' in (source,destination):
            row=self.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
            schema=self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_quotes'").fetchone()
            if not row or not schema:raise ValueError('Cross not activated')
            if row[0] != 1:raise ValueError('Trading wallet must be paused')
            if source=='futures_cross':
                from .cross_paper_book import CrossPaperBook
                cross=object.__new__(CrossPaperBook);cross.shared=self;cross.db=self.db;cross.ledger=self.ledger
                cross._check()
                current=time.time() if now is None else now
                cross._require_coverage(current)
                marks=cross._marks(cross.stored_quotes(),current)
                if quantity is None or cross._risk(marks)['available'] < amount(quantity,positive=True):
                    raise ValueError('Insufficient cross collateral')

    def propose_transfer(self,request_id,source,destination,value,*,now=None,ttl=120):
        with self.transaction():
            if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            self._assert_consistent('spot');self._assert_consistent('futures')
            if self.db.execute('SELECT 1 FROM transfer_proposals WHERE request_id=?',(request_id,)).fetchone():
                return self.ledger.propose_transfer(request_id,source,destination,value,now=now,ttl=ttl)
            self._sync_pause_and_validate(source,destination,value,now)
            return self.ledger.propose_transfer(request_id,source,destination,value,now=now,ttl=ttl)

    def confirm_transfer(self,token,*,now=None):
        with self.transaction():
            if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            before={'spot':self._assert_consistent('spot'),'futures':self._assert_consistent('futures')}
            row=self.db.execute('SELECT * FROM transfer_proposals WHERE id=?',(token,)).fetchone()
            if not row:raise ValueError('Unknown transfer confirmation')
            if row['status']=='DONE':return row['event_id']
            self._sync_pause_and_validate(row['source'],row['destination'],row['quantity'],now)
            event=self.ledger.confirm_transfer(token,now=now)
            self.fault('after_transfer_post')
            quantity=amount(row['quantity'],positive=True)
            with localcontext() as c:
                c.prec=120
                for wallet,delta in ((row['source'],-quantity),(row['destination'],quantity)):
                    market={'spot':'spot','futures_isolated':'futures'}.get(wallet)
                    if market:
                        self._set(market,'cash',encode(amount(self._get(market,'cash'))+delta))
            for market in ('spot','futures'):
                self._record_projection(market,before[market],self.snapshot(market),'shared:transfer-projection:'+token+':'+market,{'transfer':token})
            self._assert_consistent('spot');self._assert_consistent('futures')
            if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            return event

    def cancel_transfer(self,token,*,now=None):
        with self.transaction():
            if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            self._assert_consistent('spot');self._assert_consistent('futures')
            return self.ledger.cancel_transfer(token,now=now)

    def apply(self, market, operation_id, details, mutation):
        """Apply a deterministic local mutation and journal its financial delta.

        This preliminary adapter is NOT a replacement for order reservations,
        trade/fill accounting or the runtime integration; those must call into the
        same transaction and include fee/fill details before any deployment.
        """
        if market not in ('spot','futures') or not operation_id or not isinstance(details,dict):raise ValueError('Invalid operation')
        canonical=json.dumps([market,details],sort_keys=True,separators=(',', ':'),ensure_ascii=False)
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest()
        with self.transaction():
            if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            self._assert_consistent('spot');self._assert_consistent('futures')
            prior=self.db.execute('SELECT fingerprint,result FROM shared_operations WHERE id=?',(operation_id,)).fetchone()
            if prior:
                if prior['fingerprint']!=fingerprint:raise ValueError('Conflicting operation identity')
                return json.loads(prior['result'])
            before=self._assert_consistent(market)
            result=mutation(self.db)
            after=self.snapshot(market)
            self._record_projection(market,before,after,'shared:'+operation_id,{'operation':details})
            self._assert_consistent('spot');self._assert_consistent('futures')
            if not self.ledger.reconcile()['balanced']:raise ValueError('Journal integrity mismatch')
            encoded=json.dumps(result,sort_keys=True,separators=(',',':'))
            self.db.execute('INSERT INTO shared_operations VALUES(?,?,?)',(operation_id,fingerprint,encoded))
            return result
