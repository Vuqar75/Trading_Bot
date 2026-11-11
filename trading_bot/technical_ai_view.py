"""Read-only numerical/pattern ML panel. No model/network/budget execution."""
import datetime as dt,json,math,time
from pathlib import Path
NAMES={'engulf_long':'бычье поглощение','engulf_short':'медвежье поглощение','pin_long':'бычий пинбар','pin_short':'медвежий пинбар',
       'inside_long':'пробой внутреннего бара вверх','inside_short':'пробой внутреннего бара вниз','break_long':'пробой20свечей вверх','break_short':'пробой20свечей вниз'}


def render(path,now=None):
    try:
        p=Path(path)
        if p.is_symlink() or p.stat().st_size>30000:raise ValueError('report_size')
        d=json.loads(p.read_bytes());now=time.time() if now is None else now
        if d.get('feature_version')!='technical-patterns-v1' or d.get('venue')!='binance-usdm' or d.get('symbol') not in ('BTCUSDT','ETHUSDT','SOLUSDT'):raise ValueError('report_schema')
        signal=d['signal_ms'];expiry=d['expires_ms'];asof=d['as_of_ms'];ml=d['ml'];f=d['features'];patterns=d['detected_patterns']
        if not all(type(x) is int for x in (signal,expiry,asof)) or expiry!=signal+900000 or not signal<=asof<expiry or asof/1000>now+5:raise ValueError('report_time')
        if ml.get('trade_allowed') is not False or ml.get('action') not in ('CANDIDATE','NO_TRADE') or not isinstance(patterns,list) or any(x not in NAMES for x in patterns):raise ValueError('report_admission')
        def number(key,scale=1):
            x=f[key]
            if type(x) not in (int,float) or not math.isfinite(x):raise ValueError('report_number')
            return format(x*scale,'.4g')
        stale=now*1000>=expiry
        when=dt.datetime.fromtimestamp(signal/1000,dt.timezone(dt.timedelta(hours=4))).strftime('%d.%m %H:%M Баку')
        lines=['[ИИ • теханализ и паттерны] '+d['symbol']+' USD-M,15m'+(' • УСТАРЕЛО' if stale else ''),
               'Закрытие свечи: '+when,'Паттерны: '+(', '.join(NAMES[x] for x in patterns) if patterns else 'подтверждённых нет'),
               'RSI14: '+number('rsi',100)+'; ADX14: '+number('adx',100)+'; ATR14: '+number('atr_pct',100)+'%.',
               'Объём относительно среднего: '+number('volume_ratio')+'x.',
               'Модель: локальный RandomForest; это не ответ GPT.']
        if 'p_win_estimate' in ml:
            pwin=ml['p_win_estimate'];net=ml['expected_stress_net_return']
            if not all(type(x) in (int,float) and math.isfinite(x) for x in (pwin,net)) or not 0<=pwin<=1:raise ValueError('report_model_number')
            lines+=['Оценка P(плюс): '+format(pwin*100,'.1f')+'%; ожидаемый net: '+format(net*10000,'.2f')+'б.п. после стрессовых расходов.']
        lines+=['Вывод: '+('исторический сигнал; использовать сейчас нельзя' if stale else 'исследовательский кандидат, допуска к сделке нет' if ml['action']=='CANDIDATE' else 'вход не подтверждён'),
                'Вероятности не калиброваны; прибыльность модели не подтверждена.','Кнопка читает снимок; GPT и сделки не запускаются.']
        return '\n'.join(lines)[:3000]
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        return '[ИИ • теханализ и паттерны]\nСвежего проверенного снимка нет. Вход не разрешён; модель и платный API этой кнопкой не запускаются.'
