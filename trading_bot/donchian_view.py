"""Read-only presentation of an independently published observer snapshot."""
import datetime as dt,json,time
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .display_amount import display_amount

SNAPSHOT=Path('/var/lib/donchian-public/snapshot.json')
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
def baku(value):
    return dt.datetime.fromisoformat(value).astimezone(dt.timezone(dt.timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S Баку')
def read(path=SNAPSHOT,now=None):
    now=time.time() if now is None else now
    try:
        raw=Path(path).read_bytes()
        if len(raw)>100000:raise ValueError('snapshot_too_large')
        data=json.loads(raw);status=data['status'];generated=dt.datetime.fromisoformat(data['generated_utc']).timestamp()
        if data['schema']!=1 or generated>now+5 or not isinstance(status['issues'],list) or not all(isinstance(i,str) and len(i)<=128 for i in status['issues']):raise ValueError('snapshot_invalid')
        start=dt.datetime.fromisoformat(status['start_utc'])
        if start.tzinfo is None or dt.datetime.fromisoformat(data['generated_utc']).tzinfo is None or start.timestamp()>generated:raise ValueError('time_invalid')
        if status.get('last_input_received_utc') is not None:dt.datetime.fromisoformat(status['last_input_received_utc'])
        rows=data['accounts'];accounts={(r['symbol'],r['scenario'],r['kind']):r for r in rows}
        expected={(s,c,k) for s in SYMBOLS for c in ('base','stress') for k in ('strategy','hold','cash')}
        if len(rows)!=18 or set(accounts)!=expected:raise ValueError('accounts_invalid')
        for a in rows:
            for k in ('cash','qty','realized','open_net','net'):
                v=a[k]
                if v is not None and (not isinstance(v,str) or len(v)>128 or not Decimal(v).is_finite()):raise ValueError('amount_invalid')
            if any(a[k] is None for k in ('cash','qty','realized')):raise ValueError('amount_missing')
        closed=data['closed']
        if any(type(closed.get(f'{c}:{s}')) is not int or closed[f'{c}:{s}']<0 for c in ('base','stress') for s in SYMBOLS):raise ValueError('closed_invalid')
        return data,accounts,now-generated
    except (OSError,ValueError,TypeError,KeyError,InvalidOperation,json.JSONDecodeError,OverflowError):return None,None,None

def render(view='summary',path=SNAPSHOT,now=None):
    data,accounts,age=read(path,now)
    if data is None:
        try:last=baku(json.loads(Path(path).with_name('last-success.json').read_text())['generated_utc'])
        except (OSError,ValueError,TypeError,KeyError):last='неизвестно'
        return '[Donchian60] Снимок недоступен или повреждён; последний успешный снимок: '+last+'.'
    status=data['status'];fresh=age<=1800 and not status['issues']
    labels={'protocol_halted':'расчёт заблокирован','missing_decision_windows':'пропущено окно','expected_candle_missing':'свеча отсутствует','data_stale_or_unconfirmed':'данные устарели','unfinished_quote_claim':'исполнение не подтверждено','collector_error':'ошибка источника','service_inactive':'служба остановлена','execution_unconfirmed':'исполнение не подтверждено','version_mismatch':'версия не совпадает'}
    state='актуальны' if fresh else 'задержка или блокировка: '+(', '.join(labels.get(x,'проверка: '+x) for x in status['issues']) or 'снимок устарел')
    days=max(0,int((dt.datetime.fromisoformat(data['generated_utc'])-dt.datetime.fromisoformat(status['start_utc'])).total_seconds()//86400))
    lines=['[Donchian60] Изолированное paper-наблюдение — реальные деньги не используются.',f"Старт: {baku(status['start_utc'])}; дней: {days}.",f"Оценка: {baku(data['generated_utc'])}; данные: {state}."]
    if view=='health':
        lines += [f"Источник: {status.get('source_protocol','неизвестно')}; последняя загрузка: {baku(status['last_input_received_utc']) if status.get('last_input_received_utc') else 'неизвестно'}.",f"Пропущенные окна: {status.get('missed_events','неизвестно')}; резервная копия: {status.get('backup',{}).get('state','неизвестно')}; восстановление: {status.get('restore',{}).get('state','неизвестно')}."]
        candles=status.get('latest_candle_utc',{})
        lines.append('Последняя дневная свеча: '+', '.join(f"{s[:-4]} {baku(candles[s]) if candles.get(s) else 'неизвестно'}" for s in SYMBOLS)+'.')
    else:
        scenarios=('base','stress') if view in ('assets','baseline') else ('base',)
        for scenario in scenarios:
            lines.append('Базовые издержки:' if scenario=='base' else 'Повышенные издержки (отдельный сценарий):')
            for s in SYMBOLS:
                a=accounts[(s,scenario,'strategy')];asset=s[:-4];qty=display_amount(a['qty'],asset);closed=data['closed'][f'{scenario}:{s}']
                realized=display_amount(a['realized']);unreal=display_amount(a['open_net']) if fresh else 'не подтверждён';net=display_amount(a['net']) if fresh else 'не подтверждён'
                lines.append(f'{asset}: позиция {qty} {asset}; закрыто сделок {closed}; реализовано {realized}, открытый PnL {unreal}, net {net} USDT.')
                if view=='baseline':
                    for kind,label in (('cash','Cash'),('hold','Buy-and-hold')):
                        b=accounts[(s,scenario,kind)];value=display_amount(b['net']) if fresh else 'не подтверждён';lines.append(f'  {label}: net {value} USDT.')
            market=[accounts[(s,scenario,'strategy')] for s in SYMBOLS]
            total=display_amount(sum((Decimal(a['net']) for a in market),Decimal(0))) if fresh and all(a['net'] is not None for a in market) else 'не подтверждён'
            lines.append(f'Итого net трёх отдельных счетов: {total} USDT.')
    lines.append('Промежуточный результат не доказывает прибыльность.')
    return '\n'.join(lines)
