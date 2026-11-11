"""Read-only numerical/pattern ML panel. No model/network/budget execution."""
import datetime as dt,json,math,time
from pathlib import Path
NAMES={'engulf_long':'бычье поглощение','engulf_short':'медвежье поглощение','pin_long':'бычий пинбар','pin_short':'медвежий пинбар',
       'inside_long':'пробой внутреннего бара вверх','inside_short':'пробой внутреннего бара вниз','break_long':'пробой20свечей вверх','break_short':'пробой20свечей вниз'}


def render_plan(path,now=None):
    """Read saved arithmetic only; never fetch a quote or create a trade."""
    try:
        p=Path(path)
        if p.is_symlink() or p.stat().st_size>30000:raise ValueError('report_size')
        d=json.loads(p.read_bytes());now=time.time() if now is None else now
        if 'trade_scenarios' not in d:
            return '[ИИ • расчёт сделки]\nДля этого снимка расчёт не сохранён. Кнопка не запускает модель или сбор данных.'
        from research.technical_trade_scenarios import validate
        plan=validate(d['trade_scenarios'],d)
        if d['as_of_ms']/1000>now+5:raise ValueError('report_time')
        stale=now*1000>=d['expires_ms']
        when=dt.datetime.fromtimestamp(d['signal_ms']/1000,dt.timezone(dt.timedelta(hours=4))).strftime('%d.%m %H:%M Баку')
        lines=['[ИИ • условный расчёт сделки] '+d['symbol']+(' • УСТАРЕЛО' if stale else ''),
               'Свеча 15m: '+when+'. Это не открытая позиция.']
        if plan['status']=='no_unambiguous_pattern':
            lines.append('Однозначного LONG/SHORT-паттерна нет — уровни сделки не строятся.')
        else:
            def fmt(v):return format(float(v),'.8g')
            lines+=['Направление паттерна: '+plan['side']+'.',
                    'Основа: закрытие '+fmt(plan['reference_close'])+' USDT, не текущая цена исполнения.',
                    'Стоп: '+fmt(plan['stop_distance_pct'])+'%; цель: 2R; удержание: до 4 часов после условного входа.',
                    'Сравнение на 100 USDT условного оборота входа; это не размер заявки.']
            for item in plan['scenarios']:
                label='База' if item['name']=='base' else 'Стресс'
                lines+=[label+': условный вход '+fmt(item['assumed_entry'])+'; стоп '+fmt(item['stop_reference'])+'; цель '+fmt(item['target_reference'])+'.',
                        '  Net без funding: при цели '+fmt(item['net_at_target_usdt_per_100'])+' USDT; при стопе '+fmt(item['net_at_stop_usdt_per_100'])+' USDT; прибыль/риск '+fmt(item['net_reward_risk'])+'.',
                        '  Модельные расходы: комиссия 5 б.п. + spread/impact '+fmt(item['impact_bps_per_side'])+' б.п. на сторону; задержка '+str(item['assumed_delay_minutes'])+' мин.']
            if stale:lines.append('Снимок истёк; использовать уровни для нового входа нельзя.')
            lines.append('ML: '+('кандидат по порогам, торгового допуска нет.' if plan['model_candidate'] else 'вход не подтверждён; расчёт уровней этого не меняет.'))
        lines+=['Цена до spread/impact после задержки условно равна закрытию; задержка не проигрывается, будущие котировки неизвестны.',
                'Funding не включён; гэп, выход по времени и фактическое исполнение могут изменить результат. Это не прогноз прибыли.',
                'Стратегия, размер и биржевые filters не допущены этим расчётом. GPT и сделки кнопкой не запускаются.']
        return '\n'.join(lines)[:3400]
    except (OSError,ValueError,TypeError,KeyError,ArithmeticError,ImportError):
        return '[ИИ • расчёт сделки]\nПроверенного расчёта нет: снимок отсутствует или повреждён. Вход не разрешён.'


def render(path,now=None):
    try:
        p=Path(path)
        if p.is_symlink() or p.stat().st_size>30000:raise ValueError('report_size')
        d=json.loads(p.read_bytes());now=time.time() if now is None else now
        if d.get('feature_version')!='technical-patterns-v1' or d.get('venue')!='binance-usdm' or d.get('symbol') not in ('BTCUSDT','ETHUSDT','SOLUSDT'):raise ValueError('report_schema')
        signal=d['signal_ms'];expiry=d['expires_ms'];asof=d['as_of_ms'];ml=d['ml'];f=d['features'];patterns=d['detected_patterns']
        if not all(type(x) is int for x in (signal,expiry,asof)) or expiry!=signal+900000 or not signal<=asof<expiry or asof/1000>now+5:raise ValueError('report_time')
        if ml.get('trade_allowed') is not False or ml.get('action') not in ('CANDIDATE','NO_TRADE') or not isinstance(patterns,list) or any(x not in NAMES for x in patterns):raise ValueError('report_admission')
        if d.get('trade_scenarios') is not None:
            from research.technical_trade_scenarios import validate
            validate(d['trade_scenarios'],d)
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
        context=d.get('derivatives')
        if context is not None:
            from research.derivatives_context import validate
            validate(context,symbol=d['symbol'],signal_ms=signal,as_of_ms=asof)
            status=context.get('status')
            if status not in ('available','partial','unavailable'):raise ValueError('derivatives_status')
            labels={'open_interest_units':'Открытый интерес, '+d['symbol'][:-4],'oi_change_1h_pct':'OI за 1ч, %','oi_change_4h_pct':'OI за 4ч, %','oi_change_24h_pct':'OI за 24ч, %','global_account_long_short_ratio':'L/S всех счетов','top_account_long_short_ratio':'L/S счетов топ-трейдеров','top_position_long_short_ratio':'L/S позиций топ-трейдеров','taker_buy_sell_ratio':'Taker BUY/SELL'}
            facts=context.get('facts',{})
            if not isinstance(facts,dict):raise ValueError('derivatives_facts')
            lines.append('Фьючерсные метрики: '+{'available':'доступны','partial':'неполные','unavailable':'недоступны'}[status]+'; задержка15мин.')
            cut=dt.datetime.fromtimestamp(context['cutoff_ms']/1000,dt.timezone(dt.timedelta(hours=4))).strftime('%d.%m %H:%M Баку')
            lines.append('Метрики не позднее '+cut+'.')
            if context.get('missing'):lines.append('Часть источников не предоставила проверенные данные.')
            for k,label in labels.items():
                if k in facts:
                    v=facts[k]
                    if type(v) not in (int,float) or not math.isfinite(v):raise ValueError('derivatives_number')
                    lines.append(label+': '+format(v,'.4g'))
            lines.append('Это факты для анализа; текущая ML-модель их не использует. OI не определяет направление.')
        if 'p_win_estimate' in ml:
            pwin=ml['p_win_estimate'];net=ml['expected_stress_net_return']
            if not all(type(x) in (int,float) and math.isfinite(x) for x in (pwin,net)) or not 0<=pwin<=1:raise ValueError('report_model_number')
            lines+=['Оценка P(плюс): '+format(pwin*100,'.1f')+'%; ожидаемый net: '+format(net*10000,'.2f')+'б.п. после стрессовых расходов.']
        lines+=['Вывод: '+('исторический сигнал; использовать сейчас нельзя' if stale else 'исследовательский кандидат, допуска к сделке нет' if ml['action']=='CANDIDATE' else 'вход не подтверждён'),
                'Вероятности не калиброваны; прибыльность модели не подтверждена.','Кнопка читает снимок; GPT и сделки не запускаются.']
        return '\n'.join(lines)[:3000]
    except (OSError,ValueError,TypeError,KeyError,OverflowError,ImportError):
        return '[ИИ • теханализ и паттерны]\nСвежего проверенного снимка нет. Вход не разрешён; модель и платный API этой кнопкой не запускаются.'
