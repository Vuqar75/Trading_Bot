"""Paper-only observation snapshots and periodic consistent backups.

Drawdown is sampled, not the true intra-sample minimum. Missing samples are gaps.
"""
import argparse
from contextlib import closing
import tempfile
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from .runtime_backup import backup
from .testnet_observer import write_report


def snapshot(source, monitor, now=None):
    now = time.time() if now is None else now
    source, monitor = Path(source).resolve(), Path(monitor).resolve()
    if source == monitor or not source.is_file():
        raise ValueError('Separate existing runtime database required')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT key,value FROM runtime_meta')}
        if meta['identity']['mode'] != 'paper':
            raise ValueError('Paper database required')
        pending = db.execute("SELECT COUNT(*) FROM runtime_orders WHERE phase!='SETTLED'").fetchone()[0]
        fills = [json.loads(r[0]) for r in db.execute('SELECT payload FROM runtime_fills ORDER BY rowid')]
        closes = [dict(r) for r in db.execute('SELECT ts,pnl,known FROM runtime_closes ORDER BY rowid')]
        transfers = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_events'").fetchone():
            transfers = [json.loads(r[0]) for r in db.execute("SELECT details FROM ledger_events WHERE kind='TRANSFER' AND created<=? ORDER BY rowid",(now,))]
    with closing(sqlite3.connect(monitor)) as obs, obs:
        obs.execute('CREATE TABLE IF NOT EXISTS samples(ts REAL PRIMARY KEY, quote_ts REAL, equity TEXT, error TEXT, pending INTEGER NOT NULL)')
        quote = meta.get('quote') or {}
        quote_ts = quote.get('time')
        equity = None
        net_transfers = sum((Decimal(t['quantity'])*(1 if t['destination']=='spot' else -1 if t['source']=='spot' else 0) for t in transfers),Decimal(0))
        if quote_ts is not None and 0 <= now-quote_ts <= 120:
            equity = str(Decimal(meta['cash']) + Decimal(meta['position']['quantity'])*Decimal(quote['price']) - net_transfers)
        errors = {'trading':meta.get('error'),'telegram':meta.get('telegram_error')}
        obs.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?)',
                    (now,quote_ts,equity,json.dumps(errors) if any(errors.values()) else None,pending))
        rows = obs.execute('SELECT ts,quote_ts,equity,error,pending FROM samples ORDER BY ts').fetchall()
    # Paper fills charge USDT. Gross = net plus allocated entry/exit fees;
    # unclosed entry fees remain attached to the position.
    qty=Decimal(0); entry_fee=Decimal(0); allocated=Decimal(0); fees=Decimal(0)
    for f in fills:
        q=Decimal(f['qty']); fee=Decimal(f['commission'])
        if f['commissionAsset']!='USDT':
            raise ValueError('Unsupported fee asset in paper observation')
        fees+=fee
        if f['isBuyer']:
            qty+=q;entry_fee+=fee
        else:
            if not qty or q>qty:raise ValueError('Inconsistent fill sequence')
            used=entry_fee*q/qty;entry_fee-=used;qty-=q;allocated+=used+fee
    known=all(r['known'] for r in closes)
    net=sum((Decimal(r['pnl']) for r in closes),Decimal(0))
    peak=None;drawdown=Decimal(0)
    for _,_,value,_,_ in rows:
        if value is None:continue
        e=Decimal(value);peak=e if peak is None else max(peak,e);drawdown=max(drawdown,peak-e)
    gaps=sum(1 for a,b in zip(rows,rows[1:]) if b[0]-a[0]>120)
    local = datetime.fromtimestamp(now,ZoneInfo('Asia/Baku'))
    start = local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    daily_closes = [r for r in closes if start <= r['ts'] <= now]
    daily_rows = [r for r in rows if start <= r[0] <= now]
    daily_peak = None; daily_dd = Decimal(0)
    for row in daily_rows:
        if row[2] is not None:
            value=Decimal(row[2]);daily_peak=value if daily_peak is None else max(daily_peak,value);daily_dd=max(daily_dd,daily_peak-value)
    daily = {'date_baku':local.strftime('%Y-%m-%d'), 'close_records':len(daily_closes),
             'net_realized_pnl_usdt':str(sum((Decimal(r['pnl']) for r in daily_closes),Decimal(0))) if all(r['known'] for r in daily_closes) else None,
             'max_sampled_drawdown_usdt':str(daily_dd) if daily_peak is not None else None,
             'samples':len(daily_rows), 'error_samples':sum(bool(r[3]) for r in daily_rows),
             'stale_or_missing_quotes':sum(r[2] is None for r in daily_rows),
             'sample_gaps_over_120s':sum(b[0]-a[0]>120 for a,b in zip(daily_rows,daily_rows[1:])),
             'gross_realized_pnl_usdt':None,'fees_usdt':None}
    return {'daily_baku':daily, 'as_of_utc':datetime.fromtimestamp(now,timezone.utc).isoformat(),
            'first_sample_utc':datetime.fromtimestamp(rows[0][0],timezone.utc).isoformat(),
            'observed_span_seconds':now-rows[0][0], 'estimated_observed_seconds':sum(min(60,b[0]-a[0]) for a,b in zip(rows,rows[1:])),
            'samples':len(rows), 'sample_gaps_over_120s':gaps,
            'stale_or_missing_quotes':sum(r[2] is None for r in rows), 'error_samples':sum(bool(r[3]) for r in rows),
            'pending_orders':pending,'closed_positions':sum(1 for r in closes),
            'net_realized_pnl_usdt':str(net) if known else None,'gross_realized_pnl_usdt':str(net+allocated) if known else None,
            'total_fees_usdt':str(fees),'max_sampled_drawdown_usdt':str(drawdown) if peak is not None else None,
            'drawdown_limitations':'Sampled equity adjusted for internal transfers; missing/stale quotes and time between samples are not observed.',
            'orders_and_fills_journal':str(source),'position':meta['position'],'paused':meta['paused']}


def render_report(report):
    from .telegram_reports import when
    d=report['daily_baku']
    def amount(v):return 'нет подтверждённых данных' if v is None else f'{Decimal(v):+.4f} USDT'
    return '\n'.join([
        'Наблюдение Spot • BTCUSDT • виртуальные деньги',
        'Обновлён: '+when(datetime.fromisoformat(report['as_of_utc']).timestamp()),
        'ЗА СЕГОДНЯ ПО БАКУ: '+d['date_baku'],
        f"Записей выходов: {d['close_records']} (включая частичные исполнения)",
        'Результат после учтённых комиссий: '+amount(d['net_realized_pnl_usdt']),
        'Выборочная просадка внутри дня: '+amount(d['max_sampled_drawdown_usdt']),
        f"Выборок: {d['samples']}; с ошибками: {d['error_samples']}; без свежей цены: {d['stale_or_missing_quotes']}",
        f"Пропусков между выборками больше 2 минут: {d['sample_gaps_over_120s']}",
        'Комиссии и результат до комиссий отдельно за день: неизвестны; старый журнал не хранит точное время каждого исполнения.',
        'ЗА ВСЁ НАБЛЮДЕНИЕ:',
        'Начало выборок: '+when(datetime.fromisoformat(report['first_sample_utc']).timestamp()),
        'Результат после учтённых комиссий: '+amount(report['net_realized_pnl_usdt']),
        'Результат движения цены до комиссий: '+amount(report['gross_realized_pnl_usdt']),
        'Все комиссии, включая открытые позиции: '+amount(report['total_fees_usdt']),
        'Выборочная просадка: '+amount(report['max_sampled_drawdown_usdt']),
        f"Незавершённых ордеров сейчас: {report['pending_orders']}",
        'Входы: '+('на паузе' if report['paused'] else 'разрешены'),
        'Просадка измерена по выборкам; пропущенные периоды и экстремумы между ними не восстановлены. Нет данных не означает ноль.'
    ])


def write_text(path,text):
    path=Path(path)
    fd,temporary=tempfile.mkstemp(dir=path.parent,prefix='.report-')
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(text+'\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def main():
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--state-dir',required=True)
    args=p.parse_args();folder=Path(args.state_dir);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    last_backup=0
    while True:
        try:
            report=snapshot(args.db,folder/'observation.db')
            write_report(folder/'daily-report.json',report)
            day=datetime.now(ZoneInfo('Asia/Baku')).strftime('%Y-%m-%d')
            write_report(folder/('report-'+day+'.json'),report)
            write_text(folder/'daily-report.txt',render_report(report))
            write_text(folder/('report-'+day+'.txt'),render_report(report))
            if time.time()-last_backup>=3600:
                backup(args.db,folder/'backup-latest.db');last_backup=time.time()
            error_path=folder/'monitor-error.json'
            if error_path.exists():error_path.unlink()
        except Exception as e:
            write_report(folder/'monitor-error.json',{'time':time.time(),'type':type(e).__name__})
        time.sleep(60)

if __name__=='__main__':main()
