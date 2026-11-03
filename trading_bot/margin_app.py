"""Spot margin cross paper worker; public prices only, no exchange orders."""
import argparse
import json
import logging
import signal
import threading
import time
from pathlib import Path

from .app import process_lock
from .margin_command_receiver import MarginCommandReceiver
from .margin_paper_book import MarginPaperBook, PAIRS
from .margin_paper_strategy import MarginPaperStrategy
from .margin_spot_data import MarginSpotData
from .margin_worker import process_margin
from .paper_shared_store import SharedPaperStore
from .testnet_observer import write_report
from .unified_queue import UnifiedQueue

log=logging.getLogger('margin-paper')


def cycle(shared,book,queue,receiver,strategy,reader,*,started,clock=time.time):
    process_margin(queue,receiver,book,shared,started=started,clock=clock)
    settings=book.db.execute('SELECT interval FROM margin_settings WHERE id=1').fetchone()
    if not settings:raise ValueError('Missing margin settings')
    rows={symbol:reader.snapshot(symbol,settings['interval']) for symbol in PAIRS}
    if any(q.get('source')!=MarginSpotData.HOST or q.get('price_kind')!='last_public_trade' for q in rows.values()):
        raise ValueError('Untrusted margin price source')
    candles={symbol:row.pop('candles') for symbol,row in rows.items()}
    now_ms=int(clock()*1000)
    return strategy.tick(rows,candles,now_ms=now_ms,clock_ms=lambda:int(clock()*1000))


def main():
    parser=argparse.ArgumentParser(description='Spot margin cross paper; no order API')
    parser.add_argument('--db',required=True);parser.add_argument('--queue',required=True)
    parser.add_argument('--poll-seconds',type=int,default=10);parser.add_argument('--once',action='store_true')
    parser.add_argument('--hold-resume',action='store_true')
    args=parser.parse_args()
    if not 5<=args.poll_seconds<=300:raise SystemExit('Invalid polling interval')
    path=Path(args.db).resolve(strict=True);queue_path=Path(args.queue).resolve(strict=True)
    if path==queue_path:raise SystemExit('Queue and financial DB must be distinct')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    stop=threading.Event()
    def shutdown(signum,frame):stop.set()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    with process_lock(str(path)+'.margin.lock'):
        shared=SharedPaperStore(path);queue=None
        try:
            queue=UnifiedQueue(queue_path);book=MarginPaperBook(shared.ledger)
            with book.ledger.transaction():book.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
            receiver=MarginCommandReceiver(book,hold_resume=True);strategy=MarginPaperStrategy(book);reader=MarginSpotData()
            started=time.time();error=None
            while not stop.is_set():
                try:
                    cycle(shared,book,queue,receiver,strategy,reader,started=started)
                    with book.ledger.transaction():
                        book._check();book.db.execute("UPDATE margin_runtime SET value='null' WHERE key='error'")
                    error=None;receiver.hold_resume=args.hold_resume;receiver.protection_checked_ms=strategy.last_protection_ms
                except Exception as exc:
                    log.error('Margin paper cycle failed: %s',type(exc).__name__)
                    book.fail_closed(type(exc).__name__);error=type(exc).__name__;receiver.hold_resume=True;receiver.protection_checked_ms=None
                now=time.time()
                try:book._marks(book.stored_quotes(),int(now*1000));quotes_fresh=True
                except (ValueError,TypeError,KeyError):quotes_fresh=False
                protection_fresh=(strategy.last_protection_ms is not None and 0<=int(now*1000)-strategy.last_protection_ms<=120000)
                write_report(path.parent/'margin-health.json',{'time':now,'mode':'spot-margin-cross-paper',
                    'paused':bool(book.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]),
                    'error':error,'positions':len(book._positions()),'quotes_fresh':quotes_fresh,
                    'protection_fresh':protection_fresh,'protection_checked_at':strategy.last_protection_ms,
                    'resume_held':receiver.hold_resume})
                if args.once:
                    if error:raise SystemExit(1)
                    break
                stop.wait(args.poll_seconds)
        finally:
            if queue:queue.close()
            shared.close()


if __name__=='__main__':main()
