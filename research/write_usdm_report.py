"""Render the fixed USD-M screen and compact, inspectable numerical results."""
import csv,gzip,hashlib,io,json,sys
from pathlib import Path
from research.intraday_spot import SEGMENTS,SYMBOLS,stamp
from research.usdm_screen import FRAMES,allowed,H
src=Path(sys.argv[1]);d=json.loads(src.read_text());r=d['results'];lines=[]
prior=json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv)>2 else None
def add(x=''):lines.append(x)
def n(x,p=3):return '—' if x is None else f'{x:.{p}f}'
def rows(tf,fam,side,scenario,symbol=None):
 return [r[f'{tf}|{fam}|{side}|{s}|{seg[0]}|{scenario}'] for s in ((symbol,) if symbol else SYMBOLS) for seg in SEGMENTS]
def summary(tf,fam,side,scenario,symbol=None):
 rr=rows(tf,fam,side,scenario,symbol);pop=[x for x in rr if x['entries']];
 return {'net':sum(x['net'] for x in rr),'gross':sum(x['gross'] for x in rr),'fees':sum(x['fees'] for x in rr),'impact':sum(x['impact'] for x in rr),'funding':sum(x['funding'] for x in rr),'turnover':sum(x['turnover'] for x in rr),'closed':sum(x['closed'] for x in rr),'entries':sum(x['entries'] for x in rr),'dd':max(x['max_dd_pct'] for x in rr),'streak':max(x['loss_streak'] for x in rr),'liq':sum(x['liquidations'] for x in rr),'ambiguous':sum(x['ambiguous_funding'] for x in rr),'without_best':sum(x['net'] for x in rr)-max((x['best_trade'] for x in pop),default=0)}
def status(tf,fam):
 s=summary(tf,fam,'BOTH','stress');btc=summary(tf,fam,'BOTH','stress','BTCUSDT');segcounts=[sum(r[f'{tf}|{fam}|BOTH|{a}|{p[0]}|stress']['closed'] for a in SYMBOLS) for p in SEGMENTS]
 if s['closed']<30 or btc['closed']<10 or min(segcounts)<5:return 'недостаточно данных'
 assets=[summary(tf,fam,'BOTH','stress',a)['net'] for a in SYMBOLS]
 periods=[sum(r[f'{tf}|{fam}|BOTH|{a}|{p[0]}|stress']['net'] for a in SYMBOLS) for p in SEGMENTS]
 if (s['net']<=0 or btc['net']<=0 or s['without_best']<=0 or sum(assets)-max(assets)<=0 or sum(x>0 for x in assets)<2 or sum(x>0 for x in periods)<3 or s['dd']>2 or s['streak']>12 or btc['entries']/(669/365)<12):return 'отклонено'
 return 'требует проверки исполнения'
add('# USD‑M perpetual: карта интервалов и стратегий')
add();add('**Решение: кандидатов для запуска нет.** В предварительно заданной выборке некоторые длинные ячейки имеют положительный итог, но не проходят требования к числу наблюдений и устойчивости. Проверенных maker-данных нет, а текущий публичный фильтр BTCUSDT `MIN_NOTIONAL=50 USDT` запрещает новый вход с установленным лимитом 10 USDT. Аналогичный минимум ETH — 20 USDT. Размер намеренно не увеличивался. Данные 2024–2026 ранее использовались проектом и не являются независимым тестом.')
add();add('План зафиксирован до первого screening: [правила 5m/15m](USDM_INTRADAY_PLAN.md), [расширенная матрица](USDM_TIMEFRAME_MATRIX_PLAN.md). Пять семейств: A пробой, B откат в тренде, C возврат к среднему, D импульс с объёмом, E spread/depth maker. Для A–D — одна конфигурация на применимую ячейку, LONG, SHORT и совмещённая позиция раздельно. У E нет данных очереди, поэтому численный maker-backtest не выполнялся. Проверено 46 ячеек A–D: множественный выбор повышает риск случайно удачного результата. Новые варианты и настройка не добавлялись.')
add();add('## Официальные интервалы и покрытие')
add();add('Документация Binance: [USD-M kline endpoint](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) и [public-data архивы](https://github.com/binance/binance-public-data); проверено 07.11.2026. Четыре периода 2025-01–2026-10 являются данными разработки; 2024-12 — прогрев. Недостаточность ниже относится к закреплённому диапазону, не к утверждению, что у Binance вообще нет более ранней истории.')
add();add('| Интервал | Наблюдений в закреплённой выборке, примерно | Прогрев | Семейства | Предполагаемое максимальное удержание | BTC входов BOTH base / год |')
add('|---|---:|---|---|---|---:|')
duration=stamp('2026-11-01')-stamp('2024-12-01')
for tf,(span,n6,n3,n12) in FRAMES.items():
 anchor=4*24*H if tf=='1w' else 0;offset=(span-(stamp('2024-12-01')-anchor)%span)%span
 count=(duration-offset)//span;fs=' '.join(allowed(tf));x=summary(tf,'A','BOTH','base','BTCUSDT')
 holding=(2*n6*span/H);holding=12 if tf in ('5m','15m') else holding
 holds=f'A до {n(holding,0)} ч; B до {n(n6*span/H,0)} ч'
 if 'C' in allowed(tf):holds+='; C до '+n(6 if tf in ('5m','15m') else n6*span/H,0)+' ч'
 if 'D' in allowed(tf):holds+='; D до 2 ч'
 add(f'| {tf} | {count} | {n12} баров ({n(n12*span/H,0)} ч) | {fs}; E: нет стакана | {holds} | {n(x["entries"]/(669/365),1)} |')
add('| 1M | 23 календарных месяца | 60 месяцев; недостаточно | A: недостаточно прогрева; E: нет стакана | A до 120 календарных месяцев | — |')
add();add('Неполные крайние 3d/1w бары исключены; 1w агрегируется с понедельника UTC. Пятиминутные архивы не использовались для восстановления минутных: для 1m/3m применены 1m USD‑M архивы и их тройки. Таблица правил и длительностей для всех семейств приведена в плане. B/C/D не назначались неподходящим долгим интервалам; отсутствие вычисления не является проигрышем.')
add();add('## Полная таблица фактически проверенных ячеек')
add();add('Ячейки показывают **base / stress net USDT (закрытых сделок base)** по сумме трёх отдельных 1000-USDT счетов и четырёх независимо начинающихся сегментов. LONG и SHORT торгуются на отдельных счетах; BOTH — собственный однопозиционный расчёт, их нельзя складывать. BTC stress указан отдельно. В таблице нет доходности одного счёта: суммарный PnL не делится на один 10-USDT вход.')
add();add('| ТФ | Семейство | LONG base / stress (n) | SHORT base / stress (n) | BOTH base / stress (n) | BTC BOTH stress (n) | Статус |')
add('|---|---|---:|---:|---:|---:|---|')
for tf in FRAMES:
 for fam in allowed(tf):
  blocks=[]
  for side in ('LONG','SHORT','BOTH'):
   b=summary(tf,fam,side,'base');s=summary(tf,fam,side,'stress');blocks.append(f'{n(b["net"])} / {n(s["net"])} ({b["closed"]})')
  btc=summary(tf,fam,'BOTH','stress','BTCUSDT')
  add(f'| {tf} | {fam} | {blocks[0]} | {blocks[1]} | {blocks[2]} | {n(btc["net"])} ({btc["closed"]}) | {status(tf,fam)} |')
add();add('`недостаточно данных` означает, в частности, менее 30 закрытых сделок в трёх активах, менее 10 BTC либо менее 5 в одном из сегментов. `Отклонено` означает провал предварительных числовых ограничений. Даже численно положительная ячейка не преодолевает несверенные версии данных и текущий BTC минимум; положительный итог без достаточной выборки не является финалистом. 1M: недостаточно прогрева; E: недостаточно доказательств исполнения для всех интервалов.')
add();add('Например, 3d A в stress дал +1,180 USDT суммарно, но BTC имел лишь 9 закрытий, а в двух сегментах было 1 и 3 сделки на всех активах. После исключения лучшего актива результат отрицателен. Это недостаточно наблюдений, а не основание выбрать победителя.')
counts=json.loads(Path('research/usdm_signal_counts.json').read_text())
if counts['input_manifest_sha256']!=d['input_manifest_sha256']:raise ValueError('signal count source mismatch')
add();add('## Частота сигналов BTC и доступный прогрев')
add();add('Это все сигналы на закрытых свечах до фильтров позиции, срока, цены и капитала; они не равны сделкам. Частота пересчитана за фактические 669 дней, отдельные периоды и ETH/SOL сохранены в `research/usdm_signal_counts.json`. Eligible — число закрытий с полностью доступным прогревом по закреплённой истории.')
add();add('| ТФ | Семейство | Eligible BTC закрытий | LONG сигналов / год | SHORT сигналов / год | BTC BOTH входов base / год |')
add('|---|---|---:|---:|---:|---:|')
for tf in FRAMES:
 for fam in allowed(tf):
  cells=[counts['counts'][f'{tf}|{fam}|BTCUSDT|{p[0]}'] for p in SEGMENTS];entries=summary(tf,fam,'BOTH','base','BTCUSDT')['entries']
  add(f"| {tf} | {fam} | {sum(x['eligible_closes'] for x in cells)} | {n(sum(x['long_signals'] for x in cells)/(669/365),1)} | {n(sum(x['short_signals'] for x in cells)/(669/365),1)} | {n(entries/(669/365),1)} |")
add();add('## Основные 5m/15m: BTC отдельно, стороны и экономика')
add();add('| ТФ / семейство | Сторона | Закрыто base | Gross base | Fee base | Spread/slip base | Funding base | Net base | Net stress | Оборот base | Max DD stress | Серия stress | Без лучшей сделки stress |')
add('|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
for tf in ('5m','15m'):
 for fam in ('A','C'):
  for side in ('LONG','SHORT','BOTH'):
   b=summary(tf,fam,side,'base','BTCUSDT');s=summary(tf,fam,side,'stress','BTCUSDT')
   add(f'| {tf} {fam} | {side} | {b["closed"]} | {n(b["gross"])} | {n(b["fees"])} | {n(b["impact"])} | {n(b["funding"],4)} | {n(b["net"])} | {n(s["net"])} | {n(b["turnover"],1)} | {n(s["dd"],3)}% | {s["streak"]} | {n(s["without_best"])} |')
add();add('В CSV включены все активы, сегменты и сценарии: gross/net, комиссии, spread/slippage, funding, оборот, экспозиция, средний net/gross/расход сделки, ожидание между входами, просадка, серии, причины выхода, отказы, MTM, ликвидация и неоднозначный funding. Ожидание входа — интервалы между фактическими входами; незавершённый интервал после последнего входа не учтён. Для каждого периода данные начинаются с cash; непрерывной многолетней кривой и гарантии частоты нет.')
add();add('## Короткие интервалы: движение и полная стоимость BTC')
add();add('Base; средние б.п. относятся к первоначальному notional завершённой сделки. Gross — направленное изменение до расходов; отрицательное движение не становится преимуществом из-за большого модуля. `Стоимость / |gross|` сравнивает совокупные fees и impact с модулем совокупного gross, включая отметку открытого остатка; при нулевом gross отношение не определено. Это описание расходов, не причинная атрибуция.')
add();add('| ТФ | Семейство | Сторона | Сделок | Средний gross, б.п. | Комиссия и spread/slip, б.п. | Funding, б.п. | Net, б.п. | Затраты / модуль gross |')
add('|---|---|---|---:|---:|---:|---:|---:|---:|')
for tf in ('1m','3m','5m','15m','30m'):
 for fam in ('A','B','C','D'):
  for side in ('LONG','SHORT'):
   rr=rows(tf,fam,side,'base','BTCUSDT');closed=sum(x['closed'] for x in rr);z=summary(tf,fam,side,'base','BTCUSDT')
   def weighted(k):return sum(x[k]*x['closed'] for x in rr if x[k] is not None)/closed if closed else None
   gross=weighted('mean_gross_bps');cost=weighted('mean_cost_bps');net=weighted('mean_bps');fund=net-gross+cost if closed else None
   ratio=(z['fees']+z['impact'])/abs(z['gross']) if abs(z['gross'])>1e-12 else None
   add(f'| {tf} | {fam} | {side} | {closed} | {n(gross,2)} | {n(cost,2)} | {n(fund,2)} | {n(net,2)} | {n(ratio,2)}× |')
add();add('## Исполнение, тариф и влияние SHORT')
add();add('По [официальной таблице Binance](https://www.binance.com/en-BH/fee/futureFee) базовый regular USDT‑M: maker 0,0200%, taker 0,0500% на сторону; BNB/VIP и промо не предполагались. Публичная страница динамическая; это опубликованный тариф на момент проверки, не установленная история тарифов и не ставка конкретного владельца. [Binance о funding и liquidation clearance](https://www.binance.com/en-AE/support/faq/detail/98488a516eb84e3eb34605683dffd554). Ставка clearance в прошлом неизвестна: 1% — только тестовая модель, не подтверждённая граница биржевых издержек.')
add();add('При 10 USDT и неизменном обороте разница taker 10→5 б.п. составляет около 0,005 USDT на сторону, или 0,01 на круг до различий цены, funding и округления. Это улучшает экономику издержек, но не создаёт сигнал. Base применяет ещё 5 б.п. spread/slippage на сторону; stress — 10 б.п. и дополнительную базовую свечу задержки. Реальный bid/ask, очередь, частичные исполнения и исторические фильтры не доказаны. Текущие фильтры и необязательные исторические приближения сохранены отдельно.')
add();add('| BTC вариант | Spot LONG base | USD-M LONG 10 б.п. fee, без funding | USD-M LONG 5 б.п. fee, без funding на том же пути | USD-M LONG 5 б.п. с funding | USD-M BOTH 5 б.п. с funding |')
add('|---|---:|---:|---:|---:|---:|')
spot=json.loads(Path('research/intraday_5m_summary.json').read_text())['results']
for tf in ('5m','15m'):
 for fam,kind in (('A','breakout'),('C','range')):
  b=summary(tf,fam,'LONG','base','BTCUSDT');old=summary(tf,fam,'LONG','spot_fee_no_funding','BTCUSDT');joint=summary(tf,fam,'BOTH','base','BTCUSDT');sp=sum(spot[f'{kind}|{tf}|BTCUSDT|{seg[0]}|base']['net'] for seg in SEGMENTS)
  assert all(not x['liquidations'] and not x['rejections'].get('capital',0) for x in rows(tf,fam,'LONG','base','BTCUSDT'))
  add(f'| {tf} {fam} | {n(sp)} | {n(old["net"])} | {n(b["net"]-b["funding"])} | {n(b["net"])} | {n(joint["net"])} |')
add();add('Spot/futures сопоставлены по окнам, периодам, 5m-модели и допущению $10/$5; отличие охватывает реальные разные цены и разные сигналы, но не очищено от расхождений архивов. Промежуточная строка без funding вычитает funding из того же futures-пути: ликвидаций и отказов по капиталу в этих строках нет. Сравнение BOTH с LONG показывает эффект разрешённой короткой стороны вместе с конкуренцией за единственную позицию, а не независимую прибыль SHORT. Все эти величины гипотетические: текущий BTC минимум не проходит.')
example_old=summary('5m','A','LONG','spot_fee_no_funding','BTCUSDT');example_base=summary('5m','A','LONG','base','BTCUSDT');example_both=summary('5m','A','BOTH','base','BTCUSDT')
add();add(f"Например, для BTC 5m-пробоя снижение taker с 10 до 5 б.п. на сопоставимом пути улучшило net без funding примерно на {n(example_base['net']-example_base['funding']-example_old['net'])} USDT; funding этого пути составил {n(example_base['funding'],3)} USDT. Разрешение обеих сторон изменило итог с {n(example_base['net'])} до {n(example_both['net'])} USDT: преимущество комиссии не компенсировало издержки и результаты дополнительных сделок. Это сравнение симуляций, не эффект одного независимого решения.")
add();add('## Плечо и риск')
add();add('Нет финалиста для полного повторного leverage-backtest. При фиксированном notional $10 маржа 1x/2x/3x равна $10/$5/$3,33, но gross/net позиции и издержки остаются прежними до различий ликвидации: плечо не исправляет отрицательное ожидание. При одинаковой марже $10 notional становится $10/$20/$30, поэтому абсолютный PnL, fees, funding и риск увеличиваются примерно в 1/2/3 раза. Условный gross +1% цены на $10 — +$0,10: это 0,01% счёта $1000 при любом плече и 1%/2%/3% от соответствующей маржи. Это арифметика, не прогноз.')
add();add('При одинаковом риске размер ограничивается дистанцией стопа, доступным капиталом и лимитом $10; увеличение плеча само по себе не увеличивает разрешённый notional. Суммарный результат −1 USDT на одном отдельном счёте $1000 соответствует −0,1% счёта; это не −10% доходности маржи одного входа. Модельная maintenance 0,5% и clearance 1% не воспроизводят точные исторические tiers; mark-extreme имеет приоритет при конфликте со стопом, а выход моделируется по trade-extreme. Скачки, частичное исполнение и реальная цена ликвидации остаются неизвестными. В выполненных 1x сценариях модельных ликвидаций не обнаружено; отсутствие ликвидаций в OHLC не является гарантией.')
add();add('Иллюстрация той же изолированной модели, не биржевая котировка: при входе 100, начальной комиссии 0,05%, maintenance 0,5% и нулевом funding только для этого арифметического примера порог mark для LONG составляет примерно 0,05 / 50,30 / 67,05 при 1x / 2x / 3x; для SHORT — 198,96 / 149,20 / 132,62. Реальный funding, tier, проскальзывание и clearance меняют результат; ранее неизвестный исторический funding нулём не заменялся.')
add();add('## Качество данных и ограничения')
add();add('Получены 207 месячных 5m и 138 месячных 1m архивов USD-M trade/mark/funding и 24 проверенных REST-дополнения пропущенных дней. 1m и 5m: соответственно 1 008 000 и 201 600 полностью закрытых баров каждого типа на актив; funding — 2100 событий на актив без пропущенных восьмичасовых слотов. Сверены хеши, UTC, последовательность, границы и конечность цены/объёма/rate. funding внутри stop-бара имеет неоднозначный порядок: возможный дебет включён, неподтверждённый кредит не утверждается; такие записи в CSV помечены и net здесь является осторожной оценкой, а не точным фактом.')
audit=json.loads(Path('research/usdm_interval_audit.json').read_text())
add();add('При точной сверке 1m→5m без допуска расхождения OHLC: '+', '.join(k.replace('|',' ')+': '+str(v['different_ohlc']) for k,v in audit.items())+' из 201 600 сравнений каждого типа. Объём между интервалами отдельно не сверялся. BTC 5m→сохранённые 15m: trade 2 несовпадения из 67 200, mark 0. На 2025-01-29 01:30 UTC архив 5m имеет open/high 101770,1, а прежний 15m и повторный REST того же trade endpoint — open 101627,4/high 101700; повторный REST 5m поддержал 15m-версию. На 2025-08-29 06:30 UTC 5m open 110966,4 против 15m open 110966,3; повторный REST воспроизвёл эту разницу интервалов. Причина предыдущих live-конфликтов закрытий на 0,1 не установлена: совпадение величины не доказывает ту же причину. Live-блокировку не меняли.')
add();add('REST и архив закреплены как разные версии, без произвольного допуска. Список и хеши: `research/usdm_1m_manifest.json`, `research/usdm_5m_manifest.json`, `research/usdm_interval_audit.json`, `research/usdm_version_audit.json`, `research/usdm_rest_audit.json`. Неполный архив не превращался в нулевой funding или синтетическую цену. Данные стакана из одной публичной пробы не доказывают maker-очередь; source time bookTicker и время события depth в ней опередили местное получение примерно на 0,8 и 1,1 секунды. Свежее событие не подтверждено: возможны различия часов и асинхронность. Подготовлен ограниченный `research.usdm_book_capture` для существующего исследовательского контура, без ключей, таймера и автоматического запуска; REST-снимки асинхронны.')
add();add('Ручная ограниченная проверка сборщика: `python3 -B -m research.usdm_book_capture --output /path/new-snapshot.jsonl --count 12 --interval 5`. Файл создаётся только новый, максимум 12 снимков за запуск; отсутствие source event time и время события позже получения помечаются явно. Проба не разрешает входы и не оценивает вероятность исполнения maker.')
if prior:
 add();add('После первого screening исправлена реализация D: средний объём ошибочно рассчитывался по range-окну вместо заданного entry-окна. Пересчитано только семейство D, значения A/B/C сохранены и сверены; это исправление реализации, а не подбор. SHA-256 исходного результата: `'+d.get('d_correction_from_sha256','не сохранён')+'`.')
 add();add('| Импульс D | Первый ошибочный BOTH stress | Исправленный BOTH stress |');add('|---|---:|---:|')
 for tf in ('1m','3m','5m','15m','30m'):
  old=sum(prior['results'][f'{tf}|D|BOTH|{a}|{p[0]}|stress']['net'] for a in SYMBOLS for p in SEGMENTS)
  new=summary(tf,'D','BOTH','stress')['net'];add(f'| {tf} | {n(old)} | {n(new)} |')
add();add('Полные числовые данные: `research/usdm_screen_summary.csv.gz`; `gzip -dc ...` показывает строки. Данные и параметры кэшировались по SHA-256 манифестов, двух планов и исследовательского кода. SHA-256 полного непубликуемого расчётного JSON: `'+hashlib.sha256(src.read_bytes()).hexdigest()+'`. Большие исходные архивы в Git не входят. Воспроизведение: скачать URL и сверить хеши манифестов, затем `python3 -B -m research.usdm_screen --data /path/usdm --cache /path/cache --output /tmp/usdm.json` и `python3 -B -m research.write_usdm_report /tmp/usdm.json`. Исходные JSON включают время создания, поэтому точный их хеш нового запуска изменится. Целевые тесты: `python3 -B -m unittest tests.test_usdm_risk tests.test_usdm_screen tests.test_usdm_book_capture tests.test_intraday_spot -q`.')
Path('USDM_TIMEFRAME_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
columns=['timeframe','family','side','symbol','segment','scenario']+sorted(next(iter(r.values())).keys())
s=io.StringIO(newline='');w=csv.writer(s);w.writerow(columns)
for key,value in sorted(r.items()):
 parts=key.split('|');w.writerow(parts+[json.dumps(value[k],sort_keys=True,separators=(',',':')) if isinstance(value[k],dict) else value[k] for k in columns[6:]])
Path('research/usdm_screen_summary.csv.gz').write_bytes(gzip.compress(s.getvalue().encode(),compresslevel=9,mtime=0))
provenance={k:v for k,v in d.items() if k!='results'};provenance['full_result_sha256']=hashlib.sha256(src.read_bytes()).hexdigest();provenance['rows']=len(r)
Path('research/usdm_screen_provenance.json').write_text(json.dumps(provenance,indent=2,sort_keys=True)+'\n',encoding='utf8')
