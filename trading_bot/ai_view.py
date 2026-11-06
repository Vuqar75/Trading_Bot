"""Read-only view of a separately produced model result; never calls an API."""
import datetime as dt,json,time
from pathlib import Path

REPORT=Path('/var/lib/tradingbot-ai-public/report.json')
SHADOW=Path('/var/lib/adaptive-shadow-public/status.json')
def shadow(path=SHADOW,now=None):
    try:
        if Path(path).stat().st_size>100000:raise ValueError('too_large')
        d=json.loads(Path(path).read_bytes());now=time.time() if now is None else now
        if d.get('schema')!=1 or type(d.get('checked_ms')) is not int or not 0<=now-d['checked_ms']/1000<=3600:raise ValueError('stale_shadow')
        mode=d.get('baseline');allowed=('TREND_UP','TREND_DOWN','RANGE','NO_TRADE')
        if mode not in allowed or d.get('entry_allowed') is not False or d.get('ai_mode') is not None:raise ValueError('invalid_shadow')
        def when(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone(dt.timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S Баку') if type(ms) is int else 'неизвестно'
        features=d.get('features',{});coverage=d.get('coverage','Публичные данные недоступны.')
        if not isinstance(features,dict) or not isinstance(coverage,str) or len(coverage)>1000:raise ValueError('invalid_shadow')
        lines=['[Адаптивный paper • SHADOW] BTCUSDT; торговых действий нет.',
               'Данные: '+when(d.get('data_ms'))+'; проверка: '+when(d['checked_ms']),
               'ИИ: ответа нет; безопасное состояние NO_TRADE, это не ответ модели.',
               'Алгоритмический baseline: '+mode+(' (сохранён на границе 4h)' if d.get('baseline_decision_recorded') else ' (текущий снимок; не решение задним числом)'),
               'Новые адаптивные входы запрещены: стратегии не прошли отбор; AI-исполнитель/единый бюджет недоступен.',
               'Следующий пересмотр: '+when(d.get('next_boundary_ms')),
               'Признаки: '+', '.join(k+'='+format(float(features[k]),'.4g') for k in ('close_4h','sma20_4h','volatility_20h_pct','spread_bps') if type(features.get(k)) in (int,float)),
               'Покрытие: '+coverage,'Фактический расход AI: неизвестен; shadow запросов не выполняет. Остаток бюджета здесь недоступен.',
               'Рабочий Spot: /status spot; независимое наблюдение: /donchian.']
        if d.get('decision_baseline') in allowed:lines.append('Сохранённый baseline границы: '+d['decision_baseline']+'; снимок '+str(d.get('decision_snapshot_id',''))[:12]+'.')
        for x in d.get('sources',[])[:3]:
            if not isinstance(x,dict) or not all(isinstance(x.get(k),str) and len(x[k])<=300 for k in ('source','title','url')) or not x['url'].startswith('https://'):raise ValueError('invalid_source')
            lines.append('Источник: '+x['source']+' — '+x['title']+' '+x['url']+'; опубликован '+when(x.get('published_ms'))+', получен '+when(x.get('observed_ms')))
        text='\n'.join(lines)
        if len(text)>3900:raise ValueError('report_too_long')
        return text
    except FileNotFoundError:return '[ИИ-аналитик] Модельный анализ ещё не выполнен. ИИ не управляет сделками, настройками или паузами.'
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,json.JSONDecodeError,OverflowError):return '[Адаптивный paper • SHADOW] Данные недоступны или устарели; новые входы запрещены. ИИ-ответ не подтверждён.'
def render(path=REPORT,now=None):
    try:
        if Path(path).stat().st_size>100000:raise ValueError('too_large')
        data=json.loads(Path(path).read_bytes())
        if data.get('schema')!=1 or not all(isinstance(data.get(k),str) and 0<len(data[k])<=800 for k in ('data_utc','answer_utc','metrics','coverage','summary','risks')):raise ValueError('invalid')
        stamps=[dt.datetime.fromisoformat(data[k]) for k in ('data_utc','answer_utc')]
        if any(x.tzinfo is None for x in stamps) or stamps[1]<stamps[0]:raise ValueError('invalid_time')
        now=time.time() if now is None else now
        if stamps[1].timestamp()>now+5:raise ValueError('future_answer')
        stale=now-stamps[0].timestamp()>86400 or now-stamps[1].timestamp()>86400
        fmt=lambda x:x.astimezone(dt.timezone(dt.timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S Баку')
        labels=[]
        if stale:labels.append('УСТАРЕЛО')
        if data.get('complete') is not True:labels.append('НЕПОЛНЫЙ ОТВЕТ')
        state=', '.join(labels) if labels else 'подготовлено'
        lines=['[ИИ-аналитик] '+state,'Данные: '+fmt(stamps[0])+'; ответ: '+fmt(stamps[1]),'Вычисленные метрики: '+data['metrics'],'Покрытие и пробелы: '+data['coverage'],'Вывод модели: '+data['summary'],'Риски и ограничения: '+data['risks']]
        sources=data.get('sources',[])
        if not isinstance(sources,list) or len(sources)>30:raise ValueError('invalid_sources')
        for x in sources[:3]:
            if not isinstance(x,dict) or not all(isinstance(x.get(k),str) and 0<len(x[k])<=250 for k in ('title','url','published_utc','observed_utc')) or not x['url'].startswith(('https://','http://')):raise ValueError('invalid_source')
            lines.append('Источник: '+x['title']+' — '+x['url']+'; опубликован '+x['published_utc']+'; получен '+x['observed_utc'])
        if len(sources)>3:lines.append('Показаны 3 источника из '+str(len(sources))+'.')
        lines.append('ИИ не управляет сделками, настройками или паузами.')
        text='\n'.join(lines)
        if len(text)>3900:raise ValueError('report_too_long')
        return text
    except FileNotFoundError:return shadow(now=now) if Path(path)==REPORT else '[ИИ-аналитик] Модельный анализ ещё не выполнен. ИИ не управляет сделками, настройками или паузами.'
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,json.JSONDecodeError):return '[ИИ-аналитик] Отчёт недоступен или повреждён; результат не подтверждён.'
