"""Read pair identities from a held financial snapshot, for offline trials."""
import json


def pair_snapshots(shared):
    if not shared.db.in_transaction:
        raise ValueError('Pair snapshot requires a financial read transaction')
    result={}
    for market,mode in (('spot','paper'),('futures','futures-paper')):
        identity=shared._get(market,'identity')
        version=shared._get(market,'pair_selection_version',0)
        if not isinstance(identity,dict) or identity.get('mode')!=mode or type(version) is not int or version<0:
            raise ValueError('Pair identity unavailable')
        symbol=identity.get('symbol','BTCUSDT')
        if not isinstance(symbol,str):raise ValueError('Pair symbol unavailable')
        result[market]={'symbols':[symbol],'version':version}
    if shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_runtime'").fetchone():
        from .cross_paper_book import CrossPaperBook
        book=object.__new__(CrossPaperBook);book.db=shared.db;book.shared=shared;book.ledger=shared.ledger
        symbols=book._symbols()
        row=shared.db.execute("SELECT value FROM cross_runtime WHERE key='pair_version'").fetchone()
        version=json.loads(row[0]) if row else 0
        if type(version) is not int or version<0:raise ValueError('Cross pair version unavailable')
        result['futures_cross']={'symbols':list(symbols),'version':version}
    return result
