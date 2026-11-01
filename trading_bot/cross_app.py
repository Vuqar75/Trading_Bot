"""Isolated process for the experimental USD-M cross *paper* model only.

This process has no order endpoints, exchange credentials or Telegram token.
It shares the financial DB and durable queue with the existing paper services.
"""
import argparse
import logging
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


def cycle(book, queue, receiver, strategy, sync, reader, *, started, now=None):
    now=time.time() if now is None else now
    process_cross(queue,receiver,book,started=started,now=now)
    quotes=sync.refresh(now=now)
    settings=book.db.execute('SELECT interval FROM cross_settings WHERE id=1').fetchone()
    if not settings:raise ValueError('Cross settings missing')
    candles={symbol:reader.candles(symbol,settings['interval'],quotes[symbol]['server_ms']) for symbol in SYMBOLS}
    server=max(q['server_ms'] for q in quotes.values())
    return strategy.tick(quotes,candles,server,now=now)


def main():
    parser=argparse.ArgumentParser(description='Experimental virtual USD-M cross paper only')
    parser.add_argument('--db',required=True);parser.add_argument('--queue',required=True)
    parser.add_argument('--poll-seconds',type=int,default=10);parser.add_argument('--once',action='store_true')
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
            queue=UnifiedQueue(queue_path)
            book=CrossPaperBook(shared);book.set_paused(True)
            receiver=CrossCommandReceiver(book);reader=CrossPublicData()
            sync=CrossDataSync(book,reader);strategy=CrossPaperStrategy(book)
            started=time.time();error=None
            while not stop.is_set():
                try:
                    cycle(book,queue,receiver,strategy,sync,reader,started=started)
                    error=None;book.set_runtime_error(None)
                except Exception as exc:
                    # Do not print exception bodies, URLs or potentially sensitive data.
                    log.error('Cross paper cycle failed: %s',type(exc).__name__)
                    book.fail_closed();error=type(exc).__name__;book.set_runtime_error(error)
                try:
                    check_time=time.time();book._require_coverage(check_time);book._marks(book.stored_quotes(),check_time);complete=True
                except ValueError:complete=False
                write_report(path.parent/'cross-health.json',{'time':time.time(),'mode':'futures-cross-paper',
                    'paused':bool(book.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]),
                    'error':error,'positions':len(book._positions()),'data_complete':complete})
                if args.once:break
                stop.wait(args.poll_seconds)
        finally:
            if queue:queue.close()
            shared.close()


if __name__=='__main__':main()
