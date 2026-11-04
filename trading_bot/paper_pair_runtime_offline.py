"""Restore experimental pair state from the financial journal; no services."""
import json

from .margin_paper_book import MarginPaperBook, PAIRS
from .paper_shared_store import SharedPaperStore


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
