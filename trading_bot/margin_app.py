"""Spot margin cross paper worker; public prices only, no exchange orders."""
import argparse
import json
import logging
import re
import signal
import threading
import time
import traceback
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


def cycle_diagnostic(exc, stage, symbol=None, interval=None, *, observed_at_ms=None, rows=None):
    """Return allowlisted context only; exception text and locals are private."""
    allowed={'commands','settings','fetch','provenance','candles','strategy','postcheck'}
    value={'stage':stage if stage in allowed else 'unknown',
           'exception_type':type(exc).__name__ if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}',type(exc).__name__) else 'Exception'}
    if symbol in PAIRS:value['symbol']=symbol
    if interval in ('1m','3m','5m','15m','30m','1h','2h','4h','6h','8h','12h','1d','3d','1w','1M'):value['interval']=interval
    if type(observed_at_ms) is int and 0<=observed_at_ms<2**63:value['observed_at_ms']=observed_at_ms
    if isinstance(rows,dict):
        stamps={}
        for name,row in list(rows.items())[:2]:
            if not isinstance(name,str) or not re.fullmatch(r'[A-Z0-9]{2,20}USDT',name) or not isinstance(row,dict):continue
            fields={k:row[k] for k in ('time_ms','server_ms','candle_server_ms') if type(row.get(k)) is int and 0<=row[k]<2**63}
            if fields:stamps[name]=fields
        if stamps:value['data_times_ms']=stamps
    root=Path(__file__).resolve().parent
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        path=Path(frame.filename)
        if path.parent.resolve()==root and re.fullmatch(r'[a-z_]+\.py',path.name):
            value['file']=path.name;value['line']=frame.lineno;break
    return value


def cycle(shared,book,queue,receiver,strategy,reader,*,started,clock=time.time):
    stage='commands';symbol=None;interval=None;rows={}
    try:
        process_margin(queue,receiver,book,shared,started=started,clock=clock)
        stage='settings';settings=book.db.execute('SELECT interval FROM margin_settings WHERE id=1').fetchone()
        if not settings:raise ValueError('Missing margin settings')
        interval=settings['interval'];stage='fetch'
        for symbol in book.pairs:rows[symbol]=reader.snapshot(symbol,interval)
        symbol=None;stage='provenance'
        if any(q.get('source')!=MarginSpotData.HOST or q.get('price_kind')!='last_public_trade' for q in rows.values()):
            raise ValueError('Untrusted margin price source')
        stage='candles';candles={symbol:row.pop('candles') for symbol,row in rows.items()}
        stage='strategy';now_ms=int(clock()*1000)
        return strategy.tick(rows,candles,now_ms=now_ms,clock_ms=lambda:int(clock()*1000))
    except Exception as exc:
        log.error('Margin cycle diagnostic: %s',json.dumps(cycle_diagnostic(exc,stage,symbol,interval,observed_at_ms=int(time.time()*1000),rows=rows),sort_keys=True))
        raise


def main():
    parser=argparse.ArgumentParser(description='Spot margin cross paper; no order API')
    parser.add_argument('--db',required=True);parser.add_argument('--queue',required=True)
    parser.add_argument('--poll-seconds',type=int,default=10);parser.add_argument('--once',action='store_true')
    parser.add_argument('--hold-resume',action='store_true')
    parser.add_argument('--experimental-pair',action='store_true',help='Enable experimental paper pair selection')
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
            queue=UnifiedQueue(queue_path,allow_pair_experiment=args.experimental_pair)
            pair_runner=None
            if args.experimental_pair:
                from .paper_pair_runtime_offline import ExperimentalMarginRunner
                pair_runner=ExperimentalMarginRunner(shared,queue,lambda symbols:MarginSpotData(experimental_pairs={s:s[:-4] for s in symbols if s not in PAIRS}))
                book=pair_runner.book;receiver=pair_runner.receiver;strategy=pair_runner.strategy;reader=pair_runner.reader
            else:
                book=MarginPaperBook(shared.ledger)
                receiver=MarginCommandReceiver(book,hold_resume=True);strategy=MarginPaperStrategy(book);reader=MarginSpotData()
            with book.ledger.transaction():book.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
            started=time.time();error=None
            while not stop.is_set():
                try:
                    if pair_runner:
                        try:pair_runner.pair_step(clock=time.time)
                        except Exception as exc:log.error('Margin pair selection failed: %s',type(exc).__name__)
                        finally:
                            book=pair_runner.book;receiver=pair_runner.receiver;strategy=pair_runner.strategy;reader=pair_runner.reader
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
