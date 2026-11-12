"""Read-only, bounded Telegram presentation of the published AI snapshot."""
import datetime as dt
import json
import math
import re
import time
from pathlib import Path
from .ai_view import REPORT, render

MODES={'TREND_UP':'LONG','TREND_DOWN':'SHORT','RANGE':'диапазон','NO_TRADE':'пропустить вход'}
ZONE=dt.timezone(dt.timedelta(hours=4))

def _when(value):
    try:
        d=dt.datetime.fromisoformat(value)
        if d.tzinfo is None:raise ValueError()
        return d.astimezone(ZONE).strftime('%d.%m %H:%M Баку')
    except (ValueError,TypeError,OverflowError):return 'неизвестно'

def _bounded(value,limit=400):
    if not isinstance(value,str):return 'неизвестно'
    return ' '.join(value.split())[:limit]

def render_screen(screen='main',path=REPORT,now=None):
    """Never talks to network or mutates any state; rejects invalid snapshots."""
    now=time.time() if now is None else now
    if screen=='patterns':
        from .technical_ai_view import render as render_patterns
        return render_patterns(Path(path).with_name('technical.json'),now)
    if screen=='plan':
        from .technical_ai_view import render_plan
        return render_plan(Path(path).with_name('technical.json'),now)
    if screen=='history':
        from .technical_ai_view import render_history
        return render_history(Path(path).with_name('technical-history.json'),now)
    try:
        path=Path(path)
        if path.stat().st_size>100000:raise ValueError('oversize')
        d=json.loads(path.read_bytes())
        checked=render(path,now)
        if 'повреждён' in checked or 'недоступен' in checked:raise ValueError('invalid')
        details=d.get('shadow_details') or {}
        if not isinstance(details,dict):raise ValueError('invalid')
        mode=details.get('model');base=details.get('baseline')
        if mode not in MODES or base not in MODES:
            mode=base=None
        stale='УСТАРЕЛО' in checked or d.get('complete') is not True
        expiry=d.get('expires_utc');schedule=details.get('schedule','Следующий пересмотр — по расписанию 4H; при блокировке запрос не отправляется.')
        if screen=='main':
            state='Ожидание или ошибка; текущий ответ недоступен.\n' if stale else ''
            result=('Последнее решение (срок истёк): ' if stale else 'Режим модели: ')+(MODES[mode] if mode else 'нет подтверждённого ответа')
            reasons=details.get('reasons',[])
            reason=_bounded(reasons[0],180) if isinstance(reasons,list) and reasons else 'Причина недоступна.'
            if reason!='Причина недоступна.' and not re.search('[А-Яа-яЁё]',reason):reason='Сохранённый исходный текст на английском — см. подробности.'
            data_time=details.get('previous_data_utc',d.get('data_utc')) if mode else None
            answer_time=details.get('previous_answer_utc',d.get('answer_utc')) if mode else None
            text='[ИИ • BTCUSDT • shadow-наблюдение]\n'+state+result+'\nПричина: '+reason+'\nАлгоритмический baseline: '+(MODES[base] if base else 'нет данных того же среза')+'\nДанные: '+_when(data_time)+'; ответ: '+_when(answer_time)+'\nДействует до: '+('срок истёк' if stale else _when(expiry))+'\n'+_bounded(schedule,180)+'\nLONG/SHORT — режим, не позиция и не приказ.\nИИ не управляет рабочими сделками. Рабочий Spot: /status spot.'
        elif screen=='why':
            reasons=details.get('reasons',[]);uncertainties=details.get('uncertainties',[])
            if not isinstance(reasons,list) or not isinstance(uncertainties,list):raise ValueError('invalid')
            text='[ИИ • Почему такое решение]\nАргументы модели: '+('; '.join(_bounded(x) for x in reasons[:8]) or 'не сохранены')+'\nНеопределённости: '+('; '.join(_bounded(x) for x in uncertainties[:8]) or 'не сохранены')+'\nМодель: '+MODES.get(mode,'нет данных')+'; baseline: '+MODES.get(base,'нет данных')+'. Причины различия отдельно не зафиксированы. Исходный текст может быть на английском.'
        elif screen=='metrics':
            f=details.get('features',{});f=f if isinstance(f,dict) else {}
            labels={'close_4h':('Цена закрытия 4H','USDT'), 'sma20_4h':('Средняя цена за 20 свечей 4H','USDT'), 'volatility_20h_pct':('Волатильность за 20 часов','%'), 'spread_bps':('Спред','б.п.')}
            lines=[]
            for k,(label,unit) in labels.items():
                v=f.get(k)
                if type(v) in (int,float) and math.isfinite(v):lines.append(label+': '+format(v,'.4g')+' '+unit)
            text='[ИИ • Метрики рынка]\n'+('\n'.join(lines) or 'Метрики не сохранены.')+'\nBaseline — обычный алгоритм по закрытию и средней 20 свечей 4H с порогом 1%.\nДанные: '+_when(d.get('data_utc'))+'\n'+_bounded(d.get('coverage'),500)+'\nВремя события стакана не подтверждено.'
        elif screen=='sources':
            src=d.get('sources',[])
            if not isinstance(src,list):raise ValueError('invalid')
            lines=[]
            for x in src[:3]:
                if not isinstance(x,dict) or not str(x.get('url','')).startswith('https://'):continue
                lines.append(_bounded(x.get('source','Источник не указан'),100)+' — '+_bounded(x.get('title'),180)+'\n'+_bounded(x['url'],250)+'\nОпубликовано: '+_when(x.get('published_utc'))+'; получено: '+_when(x.get('observed_utc')))
            text='[ИИ • Источники и новости]\nПереданные источники:\n'+('\n\n'.join(lines) or 'Нет подтверждённых ссылок.')+'\nКакие источники модель действительно использовала, не зафиксировано. Полнота покрытия не подтверждена; банковские новости не обязательно относятся к крипторынку.'
        elif screen=='costs':
            costs=details.get('budget_display')
            text='[ИИ • Расходы и состояние]\n'+(_bounded(costs,1500) if isinstance(costs,str) else _bounded(d.get('risks'),1000))+'\nРезерв включает неопределённые попытки; оценка известных ответов не добавляется к нему как второй расход. Фактический счёт провайдера не подтверждён.\n'+_bounded(schedule,200)
        else:raise ValueError('screen')
        return text[:3400]
    except (OSError,ValueError,TypeError,KeyError,OverflowError,json.JSONDecodeError):
        return '[ИИ • BTCUSDT • shadow-наблюдение]\nОтчёт отсутствует или повреждён; модельный результат не подтверждён. ИИ не управляет сделками. Рабочий Spot: /status spot.'
