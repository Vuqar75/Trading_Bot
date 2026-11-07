"""Write the fixed report from the fixed, already computed research output."""
import hashlib,json,sys,statistics
from collections import Counter
from pathlib import Path
from research.intraday_spot import SEGMENTS,SYMBOLS,stamp

src=Path(sys.argv[1]);data=json.loads(src.read_text());r=data['results'];lines=[]
def add(s=''):lines.append(s)
def num(x,n=3):return '—' if x is None else f'{x:.{n}f}'
def waiting(row,begin,end):
 times=sorted(round(x['t']-x['hold_h']*3600000) for x in row['trades'])
 start=stamp(begin);finish=stamp(end)
 if not times:return None,(finish-start)/3600000
 completed=[(times[0]-start)/3600000]+[(b-a)/3600000 for a,b in zip(times,times[1:])]
 return sum(completed)/len(completed),max(completed+[(finish-times[-1])/3600000])
def group(kind,tf,scenario):return [r[f'{kind}|{tf}m|{s}|{p[0]}|{scenario}'] for s in SYMBOLS for p in SEGMENTS]
def summary(kind,tf,scenario):
 rows=group(kind,tf,scenario);nets={s:sum(r[f'{kind}|{tf}m|{s}|{p[0]}|{scenario}']['net'] for p in SEGMENTS) for s in SYMBOLS};trades=[t for row in rows for t in row['trades']];closed=[t for t in trades if t['closed']]
 periods={p[0]:sum(r[f'{kind}|{tf}m|{s}|{p[0]}|{scenario}']['net'] for s in SYMBOLS) for p in SEGMENTS}
 return {'net':sum(x['net'] for x in rows),'gross':sum(x['gross'] for x in rows),'cost':sum(x['costs'] for x in rows),'turnover':sum(x['turnover'] for x in rows),'trades':len(closed),'entries':sum(x['entries'] for x in rows),'btc_entries':sum(r[f'{kind}|{tf}m|BTCUSDT|{p[0]}|{scenario}']['entries'] for p in SEGMENTS),
  'max_dd':max(x['max_dd_pct'] for x in rows),'streak':max(x['longest_loss_streak'] for x in rows),'nets':nets,'periods':periods,'without_best_trade':sum(x['net'] for x in trades)-max((x['net'] for x in trades),default=0),
  'without_best_asset':sum(nets.values())-max(nets.values()),'avg_bps':sum(x['net']/x['entry_notional']*10000 for x in closed)/len(closed) if closed else None,
  'avg_gross_bps':sum(x['gross']/x['entry_notional']*10000 for x in closed)/len(closed) if closed else None,'avg_cost_bps':sum((x['gross']-x['net'])/x['entry_notional']*10000 for x in closed)/len(closed) if closed else None,
  'median_abs_gross_bps':statistics.median(abs(x['gross'])/x['entry_notional']*10000 for x in closed) if closed else None,'median_cost_bps':statistics.median((x['gross']-x['net'])/x['entry_notional']*10000 for x in closed) if closed else None,
  'win_pct':100*sum(x['net']>0 for x in closed)/len(closed) if closed else None}

add('# BTC Spot: фиксированное сравнение 5m и 15m')
add();add('Расчёт после фиксации [плана](SPOT_5M_15M_PLAN.md), без подбора параметров. Четыре правила одинаковы по длительности окон; 2025–2026 ранее использовались проектом и не являются независимой проверкой. 2024-12 — только прогрев. Каждый сегмент стартует с cash: суммирование сегментов не является непрерывной доходностью портфеля.')
add();add('**Решение В: все четыре варианта отклонены для будущего пилота.** На каждом совокупный и BTC net отрицательны в повышенном сценарии; частота не компенсирует издержки. Пробой 5m дал больше сделок и больший убыток, чем 15m; возврат 5m также увеличил оборот и убыток. Задержка меняет выбор исполнений и в этом срезе улучшает результаты диапазона, но даже stress остаётся отрицательным: это не доказательство, что задержка полезна. Четыре проверки увеличивают риск случайного выбора; ни одна не является независимой. Отсутствие данных стакана и частичных исполнений остаётся отдельным ограничением.')
add();add('Ранее проверенные 15m: SMA10/30, SMA50/200, пробой high 20 / low 10 свечей и возврат ниже SMA20 − 2σ; у всех stop 2%, take 4%, 10 USDT и следующие open. В 2024/2025 первоначальный SMA дал суммарный gross примерно +0.63/+9.91 USDT, а издержки около 62.70/60.93 USDT на трёх счетах. Бывший дневной фильтр также отклонён. Эта работа не пересчитывает прежние результаты и применяет другие заранее заданные окна и защиту.')
add();add('## Четыре конфигурации: сумма отдельных счетов и сегментов')
add();add('| Правило | ТФ | Сделки base | Gross base | Затраты base | Net base | Net повышенные + задержка | BTC stress | Без лучшей сделки stress | Без лучшего актива stress | Макс. просадка счёта stress | Серия убытков stress |')
add('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
for kind in ('breakout','range'):
 for tf in (5,15):
  b=summary(kind,tf,'base');s=summary(kind,tf,'stress')
  add(f"| {kind} | {tf}m | {b['trades']} | {num(b['gross'])} | {num(b['cost'])} | {num(b['net'])} | {num(s['net'])} | {num(s['nets']['BTCUSDT'])} | {num(s['without_best_trade'])} | {num(s['without_best_asset'])} | {num(s['max_dd'])}% | {s['streak']} |")
add();add('Сумма PnL измеряется в USDT для трёх отдельных счетов, а не в процентах от одного входа. Незакрытые позиции оценены с расходами модельного выхода; они не считаются завершёнными сделками. Просадка и серия выше — максимум внутри отдельного сегмента, не непрерывная серия между сегментами.')
add();add('## Сценарии затрат и задержки')
add();add('| Правило | ТФ | Base net | Только рост издержек | Только задержка 5m | Оба stress | Средний gross, б.п. | Медиана модуля gross / затрат, б.п. | Средний net, б.п. | Прибыльных сделок % |')
add('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
for kind in ('breakout','range'):
 for tf in (5,15):
  b=summary(kind,tf,'base');c=summary(kind,tf,'cost_only');d=summary(kind,tf,'delay_only');s=summary(kind,tf,'stress')
  add(f"| {kind} | {tf}m | {num(b['net'])} | {num(c['net'])} | {num(d['net'])} | {num(s['net'])} | {num(b['avg_gross_bps'],2)} | {num(b['median_abs_gross_bps'],2)} / {num(b['median_cost_bps'],2)} | {num(b['avg_bps'],2)} | {num(b['win_pct'],1)} |")
add();add('Base предполагает следующую 5m open в момент границы; stress — ещё 5 минут ожидания. Это дискретная OHLC-модель, не замер задержки и не доказательство доступности цены. Спред и проскальзывание — сценарные допущения, не исторические котировки.')
rb5=summary('range',5,'base');rb15=summary('range',15,'base')
add();add(f"Медиана **модуля** gross не показывает направление: у пробоев она около 70 б.п. при расходах около 30 б.п., но положительными после затрат оказались лишь около 21% сделок. Для 5m возврата медиана движения {num(rb5['median_abs_gross_bps'],1)} б.п. при затратах около 30; у 15m она {num(rb15['median_abs_gross_bps'],1)} б.п., но положительны лишь {num(rb15['win_pct'],1)}% сделок. Средний знаковый gross всех четырёх правил близок к нулю, и этого недостаточно для покрытия затрат. Более высокое проскальзывание может также отсеять часть входов по заранее заданному ограничению расстояния до цели, поэтому изменение net не обязано быть монотонным.")
if data.get('range_correction_from_sha256'):
 add();add('Исправление модели после первичного расчёта: допуск по расстоянию до цели ошибочно использовал сырую цену, хотя план требовал цену после проскальзывания. Пересчитаны только два правила диапазона и их сценарии; пробой и baseline сохранены. До исправления net base/stress составлял −40.277/−37.410 USDT для range 5m и −24.861/−26.566 для range 15m; выше показаны исправленные значения. Правила и критерии не менялись. SHA-256 исходного полного расчёта: `'+data['range_correction_from_sha256']+'`.')
add();add('## По активам и периодам')
add();add('В таблице base: gross/net и издержки в USDT, оборот в USDT. Ожидание — среднее завершённых интервалов от начала сегмента и между входами / максимальное наблюдаемое, включая незавершённый интервал после последнего входа, в часах; это не задержка исполнения и не гарантия будущей частоты. Причины и отказы приводятся только если они наблюдались. MTM — открытый net с модельной стоимостью выхода на границе.')
add();add('| Правило | ТФ | Актив | Период | Сделки / входы | Gross / net | Затраты / оборот | Средняя сделка USDT / б.п. | В позиции % / удержание ч | DD % / серия | Входы интервал ч | Отказы | Выходы | MTM | Net stress |')
add('|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|')
for kind in ('breakout','range'):
 for tf in (5,15):
  for sym in SYMBOLS:
   for label,begin,end in SEGMENTS:
    x=r[f'{kind}|{tf}m|{sym}|{label}|base'];y=r[f'{kind}|{tf}m|{sym}|{label}|stress']
    waitmean,waitmax=waiting(x,begin,end)
    rej=', '.join(f'{k}:{v}' for k,v in sorted(x['rejected'].items())) or 'нет';reasons=', '.join(f'{k}:{v}' for k,v in sorted(x['exit_reasons'].items())) or 'нет'
    add(f"| {kind} | {tf}m | {sym[:-4]} | {label} | {x['closed_trades']} / {x['entries']} | {num(x['gross'])} / {num(x['net'])} | {num(x['costs'])} / {num(x['turnover'],1)} | {num(x['avg_trade'],4)} / {num(x['avg_bps'],1)} | {num(x['exposure_pct'],1)} / {num(x['avg_hold_h'],2)} | {num(x['max_dd_pct'],2)} / {x['longest_loss_streak']} | {num(waitmean,1)} / {num(waitmax,1)} | {rej} | {reasons} | {num(x['open_mark_net'],4)} | {num(y['net'])} |")
add();add('## Условия диапазона и переходы')
add();add('Режим назначен по ширине предыдущего 12-часового диапазона на момент входа; «переход» означает последующее закрытие ниже сохранённой нижней границы. Переходы пробоя отдельно не отслеживались, а выход вверх из диапазона здесь не классифицирован как смена режима. Это описание, не причинная оценка; stop внутри свечи мог предшествовать такому закрытию.')
add();add('| Правило | ТФ | Net при входе в диапазоне / сделки | Net вне диапазона / сделки | Переход вниз net / сделки |')
add('|---|---:|---:|---:|---:|')
for kind in ('breakout','range'):
 for tf in (5,15):
  rr=group(kind,tf,'base');a=sum(x['regimes']['range']['net'] for x in rr);b=sum(x['regimes']['trend']['net'] for x in rr);c=sum(x['range_transition']['net'] for x in rr)
  transition=f"{num(c)} / {sum(x['range_transition']['count'] for x in rr)}" if kind=='range' else 'не измерялось'
  add(f"| {kind} | {tf}m | {num(a)} / {sum(x['regimes']['range']['count'] for x in rr)} | {num(b)} / {sum(x['regimes']['trend']['count'] for x in rr)} | {transition} |")
add();add('## Сопоставимые базовые линии')
add();add('| Актив | Cash, USDT | Buy-and-hold до 10 USDT base | Buy-and-hold stress |')
add('|---|---:|---:|---:|')
for s in SYMBOLS:
 def bh(scenario):
  values=[data['baselines'][f'{s}|{p[0]}|{scenario}']['buy_hold_net'] for p in SEGMENTS]
  return sum(values) if all(x is not None for x in values) else None
 add(f'| {s} | 0 | {num(bh("base"))} | {num(bh("stress"))} |')
add();add('Buy-and-hold начинается заново в каждом сегменте и оценивается с выходными расходами; это не полностью инвестированный счёт и не непрерывное удержание.')
add();add('## Данные и ограничения')
add();add('Binance Spot 5m: 69 архивов, 2024-12–2026-10, 201600 свечей на актив. SHA-256 получен из официальных CHECKSUM и сохранён в `research/intraday_5m_manifest.json`. Проверены миллисекундные и микросекундные timestamps, UTC, полная закрытость, OHLCV, дубли и непрерывность. Тройки 5m точно совпали с 67200 проверенными по прежним хешам 15m OHLCV на каждом активе. Прежние расхождения версий USD-M к этому Spot-набору не относятся; USD-M цены сюда не включены.')
add();add('15m свечи из 5m; 5m из 15m не восстанавливались. Внутри 5m порядка цены нет: стоп при конфликте со средним получает приоритет, gap — по худшему open. Target не считается исполненным по касанию: следующий 5m open — модель taker. Оценка глубины, реального спреда, очереди и частичных исполнений отсутствует. Произведённые 5m/15m сигналы не являются доказательством исполнимого скальпинга. Исторические комиссии и фильтры аппроксимированы.')
add();add('SHA-256 полного расчётного JSON: `'+hashlib.sha256(src.read_bytes()).hexdigest()+'`. Тестовая 1m→5m→15m агрегация использует синтетические данные; исторические 1m цены для этого исследования не запрашивались.')
add();add('Проверка: `python3 -B -m unittest tests.test_intraday_spot -q`. Воспроизведение при сохранённых архивах: `python3 -B -m research.intraday_spot --data /path/spot5m --manifest research/intraday_5m_manifest.json --original-15m /path/strategy-data --output /tmp/intraday.json`, затем `python3 -B -m research.write_intraday_report /tmp/intraday.json`. Для восстановления файлов использовать URL из манифеста и сверить SHA-256; большие архивы в Git не включены. Полный JSON содержит время расчёта, поэтому его хеш при повторном запуске изменится; сравнивать числовые результаты и качество отдельно.')
Path('SPOT_5M_15M_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
Path('research/intraday_5m_manifest.json').write_text(json.dumps(data['manifest'],indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
compact={'created_utc':data['created_utc'],'quality':data['quality'],'baselines':data['baselines'],'results':{k:{a:b for a,b in v.items() if a!='trades'} for k,v in r.items()}}
Path('research/intraday_5m_summary.json').write_text(json.dumps(compact,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
