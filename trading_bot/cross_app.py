"""Isolated process for the experimental USD-M cross *paper* model only.

This process has no order endpoints, exchange credentials or Telegram token.
It shares the financial DB and durable queue with the existing paper services.
"""
import argparse
import logging
import os
import signal
import threading
import time
from pathlib import Path

from .app import process_lock
from .cross_command_receiver import CrossCommandReceiver
from .cross_data_sync import CrossDataSync
from .cross_paper_book import CrossPaperBook, SYMBOLS
from .cross_paper_strategy import CrossPaperStrategy
from .cross_public_data import CrossPublicData
from .cross_worker import process_cross
from .paper_shared_store import SharedPaperStore
from .testnet_observer import write_report
from .unified_queue import UnifiedQueue

log=logging.getLogger('cross-paper')


def cycle(book, queue, receiver, strategy, sync, reader, *, started, now=None, clock=time.time, experimental_pair=False):
    live_clock=clock if now is None else None
    now=clock() if now is None else now
    process_cross(queue,receiver,book,started=started,now=now)
    quotes=sync.refresh(now=now)
    settings=book.db.execute('SELECT interval FROM cross_settings WHERE id=1').fetchone()
    if not settings:raise ValueError('Cross settings missing')
    candles={symbol:reader.candles(symbol,settings['interval'],quotes[symbol]['server_ms']) for symbol in book._symbols()}
    server=max(q['server_ms'] for q in quotes.values())
    metadata=None;metadata_at=None
    if experimental_pair and any(s not in SYMBOLS for s in book._symbols()):
        try:
            rows=reader.get('/fapi/v1/exchangeInfo').get('symbols')
            if isinstance(rows,list):metadata={r['symbol']:r for r in rows if isinstance(r,dict) and r.get('symbol') in book._symbols()}
            metadata_at=clock() if live_clock else now
        except Exception:
            log.warning('Cross pair metadata unavailable; protection remains active')
    finished=clock() if live_clock else now
    return strategy.tick(quotes,candles,server,now=finished,clock=live_clock,metadata=metadata,metadata_at=metadata_at)


def main():
    parser=argparse.ArgumentParser(description='Experimental virtual USD-M cross paper only')
    parser.add_argument('--db',required=True);parser.add_argument('--queue',required=True)
    parser.add_argument('--poll-seconds',type=int,default=10);parser.add_argument('--once',action='store_true')
    parser.add_argument('--hold-resume',action='store_true',help='Keep paper entries paused during recovery verification')
    parser.add_argument('--experimental-pair',action='store_true',help='Enable experimental paper pair selection')
    args=parser.parse_args()
    if not 5<=args.poll_seconds<=300:raise SystemExit('Invalid polling interval')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    stop=threading.Event()
    def shutdown(signum,frame):stop.set()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    path=Path(args.db).resolve(strict=True);queue_path=Path(args.queue).resolve(strict=True)
    if path==queue_path:raise SystemExit('Queue and financial DB must be distinct')
    with process_lock(str(path)+'.cross.lock'):
        shared=SharedPaperStore(path);queue=None
        try:
            queue=UnifiedQueue(queue_path,allow_pair_experiment=args.experimental_pair)
            pair_runner=None
            if args.experimental_pair:
                from .paper_pair_runtime_offline import ExperimentalCrossRunner
                pair_runner=ExperimentalCrossRunner(shared,queue,lambda symbols:CrossPublicData(symbols=symbols,allow_pair_experiment=True))
                book=pair_runner.book;receiver=pair_runner.receiver;reader=pair_runner.reader;sync=pair_runner.sync;strategy=pair_runner.strategy
            else:
                book=CrossPaperBook(shared);receiver=CrossCommandReceiver(book,hold_resume=args.hold_resume);reader=CrossPublicData()
                sync=CrossDataSync(book,reader);strategy=CrossPaperStrategy(book)
            book.set_paused(True)
            started=time.time();error=None
            diagnostic_state={'available':None}
            def diagnostic(item):
                try:write_report(path.parent/'cross-sync-diagnostic.json',dict(item,pid=os.getpid()))
                except (OSError,ValueError,TypeError):diagnostic_state['available']=False
                else:diagnostic_state['available']=True
            while not stop.is_set():
                try:
                    if pair_runner:
                        try:pair_runner.pair_step(clock=time.time)
                        except Exception as exc:log.error('Cross pair selection failed: %s',type(exc).__name__)
                        finally:
                            book=pair_runner.book;receiver=pair_runner.receiver;reader=pair_runner.reader;sync=pair_runner.sync;strategy=pair_runner.strategy
                    sync.diagnostic=diagnostic
                    cycle(book,queue,receiver,strategy,sync,reader,started=started,experimental_pair=args.experimental_pair)
                    if pair_runner:receiver.hold_resume=args.hold_resume
                    error={'historical_unconfirmed':'HistoricalFundingUnavailable',
                           'historical_contradiction':'FundingHistoryContradiction'}.get(sync.model_status)
                    book.set_runtime_error(error)
                except Exception as exc:
                    # Do not print exception bodies, URLs or potentially sensitive data.
                    log.error('Cross paper cycle failed: %s',type(exc).__name__)
                    book.fail_closed();error=type(exc).__name__;book.set_runtime_error(error)
                check_time=time.time()
                try:
                    snapshot=book.stored_quotes()
                    book._marks({s:{**q,'funding_complete':True} for s,q in snapshot.items()},check_time)
                    quotes_fresh=True
                except (ValueError,TypeError,KeyError):quotes_fresh=False
                protection_fresh=(strategy.last_protection_at is not None and 0<=check_time-strategy.last_protection_at<=120)
                write_report(path.parent/'cross-health.json',{'time':check_time,'mode':'futures-cross-paper',
                    'paused':bool(book.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]),
                    'error':error,'positions':len(book._positions()),
                    'funding_pages':sync.page_status,'funding_paper_model':sync.model_status,
                    'funding_publication_finality':'not_guaranteed_by_source',
                    'quotes_fresh':quotes_fresh,'protection_fresh':protection_fresh,
                    'sync_diagnostics_available':diagnostic_state['available'],
                    'protection_checked_at':strategy.last_protection_at,'resume_held':args.hold_resume})
                if args.once:
                    if error:raise SystemExit(1)
                    break
                stop.wait(args.poll_seconds)
        finally:
            if queue:queue.close()
            shared.close()


if __name__=='__main__':main()
