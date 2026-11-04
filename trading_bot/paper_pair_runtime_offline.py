"""Restore experimental pair state from the financial journal; no services."""
import json

from .margin_paper_book import MarginPaperBook, PAIRS
from .paper_shared_store import SharedPaperStore


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


def restore_margin_book(shared):
    if not isinstance(shared,SharedPaperStore):raise ValueError('Shared paper store required')
    with shared.transaction():
        if not shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='margin_runtime'").fetchone():
            raise ValueError('Margin runtime unavailable')
        row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_symbols'").fetchone()
        selected=json.loads(row[0]) if row else sorted(PAIRS)
        row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_version'").fetchone()
        version=json.loads(row[0]) if row else 0
        if (not isinstance(selected,list) or not 1<=len(selected)<=2 or
            any(not isinstance(s,str) for s in selected) or len(set(selected))!=len(selected) or
            selected!=sorted(selected) or type(version) is not int or version<0):
            raise ValueError('Invalid margin pair identity')
        table=shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pair_margin_selection_events'").fetchone()
        latest=shared.db.execute('SELECT version,result FROM pair_margin_selection_events ORDER BY version DESC LIMIT 1').fetchone() if table else None
        if ((latest is None and (version!=0 or selected!=sorted(PAIRS))) or
            (latest is not None and (latest['version']!=version or json.loads(latest['result']).get('symbols')!=selected))):
            raise ValueError('Margin pair history conflicts with identity')
        rates={r[0] for r in shared.db.execute('SELECT DISTINCT asset FROM margin_loan_rates')}
        if not {'USDT','BTC','ETH'}.issubset(rates):raise ValueError('Margin asset rate unavailable')
        extra={asset+'USDT':asset for asset in rates if asset not in ('USDT','BTC','ETH')}
        if any(symbol not in {**PAIRS,**extra} for symbol in selected):raise ValueError('Margin asset rate unavailable')
        book=MarginPaperBook(shared.ledger,experimental_pairs=extra,selected_symbols=selected,expected_pair_version=version)
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
