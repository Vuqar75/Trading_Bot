"""Restore experimental pair state from the financial journal; no services."""
import json

from .margin_paper_book import MarginPaperBook, PAIRS
from .paper_shared_store import SharedPaperStore


def _restored_identity(shared,market,mode,table):
    with shared.transaction():
        identity=shared._get(market,'identity');version=shared._get(market,'pair_selection_version',0)
        if (not isinstance(identity,dict) or identity.get('mode')!=mode or
            not isinstance(identity.get('symbol'),str) or type(version) is not int or version<0):
            raise ValueError('Pair identity unavailable')
        exists=shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
        latest=shared.db.execute('SELECT version,result FROM '+table+' ORDER BY version DESC LIMIT 1').fetchone() if exists else None
        if ((latest is None and (version!=0 or identity['symbol']!='BTCUSDT')) or
            (latest is not None and (latest['version']!=version or json.loads(latest['result']).get('symbol')!=identity['symbol']))):
            raise ValueError('Pair identity conflicts with history')
        return dict(identity)


def restore_spot_store(path):
    from .runtime_store import RuntimeStore
    shared=SharedPaperStore(path)
    try:identity=_restored_identity(shared,'spot','paper','pair_spot_selection_events')
    finally:shared.close()
    return RuntimeStore(path,identity,shared=True)


def restore_isolated_store(path):
    from .futures_store import FuturesStore
    shared=SharedPaperStore(path)
    try:identity=_restored_identity(shared,'futures','futures-paper','pair_isolated_selection_events')
    finally:shared.close()
    return FuturesStore(path,shared=True,symbol=identity['symbol'],allow_pair_experiment=True,
        margin=identity['margin'],fee_bps=identity['fee_bps'],slippage_bps=identity['slippage_bps'],
        maintenance_bps=identity['maintenance_bps'])


def restore_cross_book(shared):
    from .cross_paper_book import CrossPaperBook
    if not isinstance(shared,SharedPaperStore):raise ValueError('Shared paper store required')
    book=CrossPaperBook(shared,allow_pair_experiment=True)
    with shared.transaction():
        row=shared.db.execute("SELECT value FROM cross_runtime WHERE key='pair_version'").fetchone()
        version=json.loads(row[0]) if row else 0
        if type(version) is not int or version<0:raise ValueError('Invalid cross pair version')
        book._pair_binding=version
        book._symbols();book._check()
        return book


def margin_book_view(db,ledger):
    from .margin_interest_ledger import MarginInterestLedger
    from .paper_wallet_ledger import valid_asset
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='margin_runtime'").fetchone():
        raise ValueError('Margin runtime unavailable')
    row=db.execute("SELECT value FROM margin_runtime WHERE key='pair_symbols'").fetchone()
    selected=json.loads(row[0]) if row else sorted(PAIRS)
    row=db.execute("SELECT value FROM margin_runtime WHERE key='pair_version'").fetchone()
    version=json.loads(row[0]) if row else 0
    if (not isinstance(selected,list) or not 1<=len(selected)<=2 or
        any(not isinstance(s,str) for s in selected) or len(set(selected))!=len(selected) or
        selected!=sorted(selected) or type(version) is not int or version<0):
        raise ValueError('Invalid margin pair identity')
    table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pair_margin_selection_events'").fetchone()
    latest=db.execute('SELECT version,result FROM pair_margin_selection_events ORDER BY version DESC LIMIT 1').fetchone() if table else None
    if ((latest is None and (version!=0 or selected!=sorted(PAIRS))) or
        (latest is not None and (latest['version']!=version or json.loads(latest['result']).get('symbols')!=selected))):
        raise ValueError('Margin pair history conflicts with identity')
    rates={r[0] for r in db.execute('SELECT DISTINCT asset FROM margin_loan_rates')}
    if not {'USDT','BTC','ETH'}.issubset(rates) or any(not valid_asset(a) for a in rates):raise ValueError('Margin asset rate unavailable')
    extra={asset+'USDT':asset for asset in rates if asset not in ('USDT','BTC','ETH')}
    if any(symbol not in {**PAIRS,**extra} for symbol in selected):raise ValueError('Margin asset rate unavailable')
    book=object.__new__(MarginPaperBook);book.db=db;book.ledger=ledger
    book._pairs={**PAIRS,**extra};book._tradable=frozenset(selected);book._pair_version=version
    book.loans=object.__new__(MarginInterestLedger);book.loans.db=db;book.loans.ledger=ledger;book.loans.assets=tuple(sorted(rates))
    book._check()
    return book


def restore_margin_book(shared):
    if not isinstance(shared,SharedPaperStore):raise ValueError('Shared paper store required')
    with shared.transaction():
        view=margin_book_view(shared.db,shared.ledger)
        extra={s:a for s,a in view._pairs.items() if s not in PAIRS}
        book=MarginPaperBook(shared.ledger,experimental_pairs=extra,selected_symbols=sorted(view.tradable),expected_pair_version=view._pair_version)
        book._check()
        return book


class ExperimentalMarginRunner:
    """Test-only rebinding when a confirmed pair change commits."""
    def __init__(self,shared,queue,reader_factory):
        if not queue.allow_pair_experiment:raise ValueError('Offline pair queue required')
        self.shared=shared;self.queue=queue;self.reader_factory=reader_factory
        self._bind()

    def _bind(self):
        from .margin_command_receiver import MarginCommandReceiver
        from .margin_paper_strategy import MarginPaperStrategy
        self.book=restore_margin_book(self.shared)
        self.reader=self.reader_factory(tuple(self.book.pairs))
        self.receiver=MarginCommandReceiver(self.book,hold_resume=True)
        self.strategy=MarginPaperStrategy(self.book)

    def pair_step(self,*,clock, fault=lambda stage:None):
        from .paper_pair_worker_offline import process_margin_pair
        try:
            return process_margin_pair(self.queue,self.shared,self.reader_factory,clock=clock,fault=fault)
        finally:
            row=self.shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_version'").fetchone()
            version=json.loads(row[0]) if row else 0
            if version!=self.book._pair_version:
                self._bind()


class ExperimentalCrossRunner:
    def __init__(self,shared,queue,reader_factory):
        if not queue.allow_pair_experiment:raise ValueError('Offline pair queue required')
        self.shared=shared;self.queue=queue;self.reader_factory=reader_factory
        self._bind()

    def _bind(self):
        from .cross_command_receiver import CrossCommandReceiver
        from .cross_data_sync import CrossDataSync
        from .cross_paper_strategy import CrossPaperStrategy
        self.book=restore_cross_book(self.shared)
        symbols=set(self.book._symbols())
        for row in self.shared.db.execute("SELECT details FROM ledger_events WHERE kind='CROSS_OPEN'"):
            value=json.loads(row[0]).get('symbol')
            if not isinstance(value,str):raise ValueError('Historical cross symbol unavailable')
            symbols.add(value)
        self.reader=self.reader_factory(sorted(symbols))
        self.receiver=CrossCommandReceiver(self.book,hold_resume=True)
        self.strategy=CrossPaperStrategy(self.book)
        self.sync=CrossDataSync(self.book,self.reader)

    def pair_step(self,*,clock,fault=lambda stage:None):
        from .paper_pair_worker_offline import process_cross_pair
        try:return process_cross_pair(self.queue,self.shared,self.reader_factory,clock=clock,fault=fault)
        finally:
            row=self.shared.db.execute("SELECT value FROM cross_runtime WHERE key='pair_version'").fetchone()
            version=json.loads(row[0]) if row else 0
            if version!=self.book._pair_binding:self._bind()


class ExperimentalSpotRunner:
    def __init__(self,store,queue,reader,*,amount='10',interval='15m'):
        from .runtime_exchange import PaperExchange
        if not store.shared or not queue.allow_pair_experiment:raise ValueError('Offline shared paper queue required')
        self.store=store;self.queue=queue;self.reader=reader;self.amount=amount;self.interval=interval;self.exchange=PaperExchange()
        self._bind()

    def _bind(self):
        from .app import Application
        self.app=Application(self.store,self.exchange,self.reader,amount=self.amount,interval=self.interval,allow_pair_experiment=True)

    def pair_step(self,*,clock,fault=lambda stage:None):
        from .paper_pair_worker_offline import process_spot_pair
        try:
            return process_spot_pair(self.queue,self.store.shared,self.reader,budget=self.amount,clock=clock,fault=fault)
        finally:
            if (self.store.get('identity')['symbol']!=self.app.symbol or
                self.store.get('pair_selection_version',0)!=self.app.pair_version):self._bind()


class ExperimentalIsolatedRunner:
    def __init__(self,store,queue,reader_factory):
        if not store.shared or not queue.allow_pair_experiment:raise ValueError('Offline shared paper queue required')
        self.store=store;self.queue=queue;self.reader_factory=reader_factory
        self.path=store.db.execute('PRAGMA database_list').fetchone()[2]
        self._bind()

    def _bind(self):
        from .futures_app import FuturesApplication
        self.reader=self.reader_factory(self.store.symbol)
        self.app=FuturesApplication(self.store,self.reader,allow_pair_experiment=True)

    def pair_step(self,*,clock,fault=lambda stage:None):
        from .paper_pair_worker_offline import process_isolated_pair
        try:
            return process_isolated_pair(self.queue,self.store.shared,self.reader_factory,
                budget=str(self.store.margin),clock=clock,fault=fault)
        finally:
            identity=self.store.get('identity');version=self.store.get('pair_selection_version',0)
            if identity['symbol']!=self.store.symbol or version!=self.store.pair_version:
                self.store.close();self.store=restore_isolated_store(self.path);self._bind()
