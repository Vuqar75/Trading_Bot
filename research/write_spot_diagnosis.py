"""Render diagnostic aggregates without committing raw data or trade-level logs."""
import argparse,collections,datetime as dt,json
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--diagnosis',required=True);p.add_argument('--development',required=True);p.add_argument('--independent',required=True);p.add_argument('--data',required=True);p.add_argument('--filtered',required=True);a=p.parse_args()
    d=json.loads(Path(a.diagnosis).read_text());dev=json.loads(Path(a.development).read_text())['results'];ind=json.loads(Path(a.independent).read_text());res=ind['results']
    mtimes=[(Path(a.data)/x['file']).stat().st_mtime for x in ind['manifest']]
    manifest={'manifest_created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'archive_mtime_min_utc':dt.datetime.fromtimestamp(min(mtimes),dt.timezone.utc).isoformat(),'archive_mtime_max_utc':dt.datetime.fromtimestamp(max(mtimes),dt.timezone.utc).isoformat(),'interval':'2026-01-01T00:00:00Z <= open < 2026-11-01T00:00:00Z','quality':'all intervals continuous; OHLC and close time checked; no missing or duplicate bars','files':ind['manifest']}
    Path('research/spot_independent_manifest_2026.json').write_text(json.dumps(manifest,indent=2)+'\n')
    summary={k:{kk:vv for kk,vv in v.items() if kk!='trades'} for k,v in d.items()}
    Path('research/spot_diagnosis_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    def concise(values):
        return {k:{**{kk:vv for kk,vv in v.items() if kk!='trades'},'best_trade':max((t['net'] for t in v['trades']),default=None),'worst_trade':min((t['net'] for t in v['trades']),default=None)} for k,v in values.items()}
    filtered=json.loads(Path(a.filtered).read_text())
    Path('research/spot_hypothesis_summary.json').write_text(json.dumps({'development':concise(dev),'independent':concise(res),'buy_signals_filtered':filtered},indent=2)+'\n')
    def fmt(x):return f'{x:.4f}'
    lines=['# Почему теряли деньги и одна проверка фильтра Spot','',
    '## Решение','',
    'Единственный вариант фильтра **отклонён**. На независимом январе–октябре 2026 результат отрицателен на BTC, ETH и SOL как при базовых, так и повышенных издержках. Фильтр уменьшил число сделок и убыток относительно текущего правила, но не выполнил заранее установленные критерии. Дополнительные варианты не испытывались; кандидата для prospective paper нет.','',
    'Исходный план и единственный фильтр зафиксированы до загрузки данных 2026 в [SPOT_HYPOTHESIS_PLAN.md](SPOT_HYPOTHESIS_PLAN.md), реализация до проверки — git blob `7a67423b0b8f3be8f08bb81617aa13242bd65fb7` (`research/spot_hypothesis.py`); промежуточный коммит присутствует в истории ветки. 2024–2025 — данные разработки, включая прежде отложенный 2025. В доступной истории исследования и коде не найдено настройки этого правила на 2026; непроверяемые внешние эксперименты исключить нельзя. Действующие paper-процессы наблюдали рынок в 2026, но это не использовалось здесь для подбора порога.','',
    '## Сопоставимость и происхождение убытка','',
    'Независимый диагностический пересчёт совпал с ранее сохранённым net во всех 36 комбинациях правил/активов/периодов (включая оба baseline), с допуском 1e-7 USDT. Размер позиции ограничен 10 USDT независимо от счёта; накопленные средства не увеличивают ставку, плеча нет. На конец периода оставшаяся позиция оценивается по последнему close с модельными затратами выхода и включается как `period_end`; это не реальный fill. Начало каждого периода — без позиции.','',
    'Gross ниже рассчитан по разнице исходных цен исполнения без комиссии и без spread/slippage на том же наборе модельных сделок. Net = gross − комиссии − spread/slippage; сделки могли бы отличаться при иной модели цены и стопа. Комиссия 10 bps на сторону, базовый spread/slippage 5 bps на сторону; стресс 10 bps плюс комиссия. 2024/2025: шаги BTC 0,00001, ETH 0,0001, SOL 0,001, минимум 5 USDT — допущения, не доказанные исторические фильтры. Средняя экспозиция и доля в позиции измерены по состоянию на конец свечи.','',
    'Расчёт return — net / 1000 USDT, не net / 10. Ожидание в bps — среднее по сделкам от входного номинала каждой сделки. MAE/MFE — средние нижняя/верхняя границы по экстремумам полных свечей позиции, включая свечу protective-выхода; возможны экстремумы после исполнения, поэтому это не точные внутрисвечные пути. Время — число затронутых 15m свечей, граница ± одна свеча.','',
    '| Год | Правило | Актив | Gross | Комиссия | Spread/slip | Net | Net % счёта | Оборот | Издержки % оборота | Издержки / модуль gross |',
    '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    keys=[f'{s}|{year}|{kind}' for year in (2024,2025) for kind in ('current','trend','breakout','reversion') for s in ('BTCUSDT','ETHUSDT','SOLUSDT')]
    for key in keys:
        s,y,k=key.split('|');x=d[key];turn=x['turnover'];ratio='—' if abs(x['gross'])<1e-10 else f"{x['costs']/abs(x['gross']):.2f}×"
        lines.append(f"| {y} | {k} | {s} | {x['gross']:.4f} | {x['fees']:.4f} | {x['spread_slippage']:.4f} | {x['net']:.4f} | {x['return_account_pct']:.4f} | {turn:.2f} | {x['costs']/turn*100 if turn else 0:.3f} | {ratio} |")
    lines+=['','| Год | Правило | Актив | Сделок | Отклонено: минимум / средства | В позиции % | Средняя экспозиция USDT | Средняя сделка USDT / bps | Средняя длительность ч | Открытый net на границе |',
    '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for key in keys:
        s,y,k=key.split('|');x=d[key];ts=x['trades'];n=len(ts);rej=x['rejected'];avg=sum(t['net'] for t in ts)/n if n else 0;bps=sum(t['net']/(t['qty']*t['entry_ref'])*10000 for t in ts)/n if n else 0;dur=sum(t['bars'] for t in ts)/n/4 if n else 0
        lines.append(f"| {y} | {k} | {s} | {n} | {rej.get('min_notional',0)} / {rej.get('insufficient_cash',0)} | {x['exposure_pct']:.2f} | {x['mean_exposure_usdt']:.4f} | {avg:.4f} / {bps:.2f} | {dur:.2f} | {x['open_end_pnl']:.4f} |")
    lines+=['','| Год | Правило | Актив | Выход signal / stop / take / конец | MAE / MFE, границы % | Лучшая / худшая сделка USDT | Net без лучшей | Тренд / боковик / волатильность: net |', '|---|---|---|---:|---:|---:|---:|---:|']
    for key in keys:
        s,y,k=key.split('|');x=d[key];ts=x['trades'];n=len(ts);counts=collections.Counter(t['reason'] for t in ts);stop=counts['stop']+counts['stop_gap']+counts['ambiguous_stop_first'];take=counts['take']+counts['take_gap'];mae=sum(t['mae_bound'] for t in ts)/n*100 if n else 0;mfe=sum(t['mfe_bound'] for t in ts)/n*100 if n else 0;best=max((t['net'] for t in ts),default=0);worst=min((t['net'] for t in ts),default=0);reg=[sum(t['net'] for t in ts if t['regime']==z) for z in ('trend','sideways','volatile')]
        lines.append(f"| {y} | {k} | {s} | {counts['signal']} / {stop} / {take} / {counts['period_end']} | {mae:.2f} / {mfe:.2f} | {best:.4f} / {worst:.4f} | {x['net']-best:.4f} | {' / '.join(fmt(z) for z in reg)} |")
    lines+=['','Режим фиксируется по последнему полностью закрытому UTC-дню до сигнала: модуль дневного движения ≥4% — volatile; иначе расстояние от SMA30 дневных закрытий ≥2% — trend; иначе sideways. Это не будущая доходность и не причинное объяснение. Если режим не прогрет, он обозначается unknown и не включён в эти три столбца. Расходы механически уменьшают результат; утверждать, что вся разница вызвана именно ими при другом исполнении, нельзя.','',
    'У SMA10/30 в 2024 валовой итог трёх отдельных счетов +0,626 USDT, комиссии 41,801 и spread/slippage 20,900; в 2025 соответственно +9,907, 40,617 и 20,308. Большое число переключений делает чувствительность к расходам заметной. Для breakout и mean reversion часть потерь возникает и до расходов.','',
    '## Один вариант: дневной фильтр входа','',
    'Только BUY SMA10/30 допускается, когда close последнего завершённого UTC-дня > 1,02 × SMA30 последних завершённых дневных close. SELL, stop/take, размер и издержки неизменны. В 2024 число сделок 2130 → 1023, в 2025 2083 → 699; по всем трём активам результаты кандидата оставались отрицательными.','',
    '| Период | Актив | Baseline net base / stress | Фильтр net base / stress | Сделок baseline → фильтр |', '|---|---|---:|---:|---:|']
    for y in (2024,2025):
        for s in ('BTCUSDT','ETHUSDT','SOLUSDT'):
            x=dev[f'{s}|{y}|current|5'];xx=dev[f'{s}|{y}|current|10'];z=dev[f'{s}|{y}|candidate|5'];zz=dev[f'{s}|{y}|candidate|10']
            lines.append(f"| {y} | {s} | {x['net']:.4f} / {xx['net']:.4f} | {z['net']:.4f} / {zz['net']:.4f} | {x['trade_count']} → {z['trade_count']} |")
    lines+=['','## Одна независимая проверка: 01.01–01.11.2026 UTC','',
    f"Только полностью закрытые месяцы, 15m; 30 публичных архивов и контрольные суммы в [манифесте](research/spot_independent_manifest_2026.json). Локальные времена записи архивов: {manifest['archive_mtime_min_utc']} — {manifest['archive_mtime_max_utc']}. Каждая серия проверена на последовательность, дубли, валидность OHLC и время закрытия. Источник — [Binance Public Data](https://github.com/binance/binance-public-data); spot timestamps 2026 переведены из микросекунд.",'',
    '| Актив | Правило | Net base / stress USDT | Net % счёта base | Сделок | Отказы минимум / средства | Сигналы BUY отфильтрованы | Оборот USDT | В позиции % | Средняя экспозиция | Макс. просадка % | Открытый net в конце |', '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for s in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        for n in ('current','candidate','hold','none'):
            x=res[f'{s}|2026|{n}|5'];z=res[f'{s}|2026|{n}|10']
            reject=x['rejected'];masked=filtered[f'{s}|2026'] if n=='candidate' else '—'
            lines.append(f"| {s} | {n} | {x['net']:.4f} / {z['net']:.4f} | {x['return_account_pct']:.4f} | {x['trade_count']} | {reject.get('min_notional',0)} / {reject.get('insufficient_cash',0)} | {masked} | {x['turnover']:.2f} | {x['exposure_pct']:.2f} | {x['mean_exposure_usdt']:.4f} | {x['max_dd_pct']:.3f} | {x['open_end_pnl']:.4f} |")
    stress=[res[f'{s}|2026|candidate|10'] for s in ('BTCUSDT','ETHUSDT','SOLUSDT')];total=sum(x['net'] for x in stress);best=max(t['net'] for x in stress for t in x['trades']);bestasset=max(x['net'] for x in stress)
    lines+=['','Отфильтрованные BUY — число сигналов, включая возникшие при уже открытой позиции; это не число отклонённых заявок. Отказы минимума и средств считаются отдельно при реальной модельной попытке входа.', '',f'Сумма трёх отдельных счетов при стресс-издержках: {total:.4f} USDT; без лучшей сделки: {total-best:.4f}; без лучшего актива: {total-bestasset:.4f}. Это сумма PnL, не доходность одного счёта. Критерии положительного результата, двух положительных активов и устойчивости не выполнены. Сделок: {sum(x["trade_count"] for x in stress)}.','',
    '## Ограничения и воспроизведение','',
    '15m свеча не раскрывает последовательность цен, spread, глубину и частичные исполнения; двойное касание считается stop-first. Исполнение на следующем open с фиксированными расходами — приближение, задержка не реконструирована. Проверка не распространяется на futures или margin. Никакая стратегия и пауза VM не менялись. На этой проверке гипотеза завершена отрицательным результатом; новые параметры не подбирались.','',
    '`PYTHONPATH=. python3 -B research/spot_diagnosis.py --data /путь/к/архивам --output /путь/diagnosis.json` проверяет старые архивы по зафиксированным SHA-256 без загрузки. `PYTHONPATH=. python3 -B research/spot_hypothesis.py development --data ... --output ...` и затем `independent` воспроизводят расчёт; перед повторной загрузкой и после неё сравните архивы 2026 с сохранённым манифестом. `PYTHONPATH=. python3 -B research/spot_filter_counts.py --data ... --output ...` проверяет все хеши и подсчитывает отфильтрованные сигналы; `research/write_spot_diagnosis.py --data ... --diagnosis ... --development ... --independent ... --filtered ...` строит отчёт. Тесты: `PYTHONPATH=. python3 -B -m unittest tests.test_spot_diagnosis tests.test_strategy_research -q`. Данные и журналы сделок остаются вне Git.']
    Path('SPOT_LOSS_DIAGNOSIS_2026_11_05.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
if __name__=='__main__':main()
