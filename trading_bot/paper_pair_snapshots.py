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
    if shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='margin_runtime'").fetchone():
        from .margin_paper_book import PAIRS
        row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_symbols'").fetchone()
        symbols=json.loads(row[0]) if row else sorted(PAIRS)
        row=shared.db.execute("SELECT value FROM margin_runtime WHERE key='pair_version'").fetchone()
        version=json.loads(row[0]) if row else 0
        if (not isinstance(symbols,list) or not 1<=len(symbols)<=2 or
            any(not isinstance(s,str) for s in symbols) or len(set(symbols))!=len(symbols) or
            type(version) is not int or version<0 or (version==0 and symbols!=sorted(PAIRS))):
            raise ValueError('Margin pair identity unavailable')
        if version:
            if not shared.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pair_margin_selection_events'").fetchone():
                raise ValueError('Margin pair history unavailable')
            latest=shared.db.execute('SELECT version,result FROM pair_margin_selection_events ORDER BY version DESC LIMIT 1').fetchone()
            if not latest or latest['version']!=version or json.loads(latest['result']).get('symbols')!=symbols:
                raise ValueError('Margin pair history conflicts with identity')
        result['margin_cross']={'symbols':symbols,'version':version}
    return result
