# Два разных паттерна диапазона, фиксированный development → validation

Продолжение того же поиска. Никакая отрицательная RSI2/VWAPконфигурация не
перенастраивается. Это два новых явно заданных механизма, не полное
воспроизведение прибыли авторов. До результатов фиксируются все правила.

Источники механизма, прочитанные до расчёта:
- NR7: https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/narrow-range-day-nr7
  Сжатие high-low относительно предыдущих6баров, выход из диапазона.
- Failed20barbreak: https://www.edgestacker.com/tools/library/scans/raschke/
  и https://therighttrader.com/learn/trading-strategies/turtle-soup
  Публично изложенный20баровый старыйэкстремум4+баров и возврат внутрь.
  Полная авторская intradaystoporder-реализация не заявляется.

Общее: только закрытые15m/1h/4h, исходныеBTC/ETH/SOL, 200баров реального
прогрева.2025development,2026Jan-Octтолькодляодноговыбранного. История
переиспользована; много ранее попробованных семейств означает selectionrisk,
а не гарантию независимого преимущества даже при положительном результате.

NR7: setupbarhigh-low строго меньше диапазона каждого из предыдущих6;
следующая и только следующая CLOSEDсвеча вышеsetuphigh→LONG,нижеsetuplow→SHORT.
ДляLONGclose>SMA200,дляSHORTclose<SMA200. Входпосле1/6min,только если rawopen
всё ещё строго за trigger. Stop на противоположной стороне SETUPдиапазона.
ВыходLONGпосле CLOSEDclose<EMA10,SHORTclose>EMA10, затем1/6mindelay.

Soup: предыдущие20закрытых баров, возраст ПОСЛЕДНЕГО повторения минимального/
максимального экстремума>=4бара. Новый low<старогоlow и CLOSEDclose>старогоlow
→LONG; новый high>старогоhigh и CLOSEDclose<старогоhigh→SHORT. Двойной паттерн
в одной свече отменяется. Trendfilter не добавляется. Входпосле1/6min,только
если rawopen сохраняет возврат за старыйlevel. Stop на low/high СИГНАЛЬНОЙ
уже закрытой свечи. ВыходLONGCLOSEDclose>EMA5,SHORTclose<EMA5,затем1/6min.

В обоих случаях stop относительно adversefill строго>0 и<=2%, иначе reject
ДО платы/открытия. Это известный уровень до входа, не будущий intrabar экстремум.
Stop gap и mark/funding/ликвидация через прежний финансовый Account. Без TP2R,
усреднения и отключения защиты. Maxhold6h/12h/24h по TF, timeexitнаdeadlineopen.
Fee5bps/side,impact5/10bps; отдельные1000USDTсчета,1x,риск<=5; BTC0.001<=150
ранее разрешённогоresearchcap,ETH/SOL<=100. Рабочие caps и паузы не меняются.

Всего18вариантов(2family×3TF×3asset),36base/stressсчетовdevelopment2025.
Критерии как предыдущие фиксированные пакеты:>=30закрытых/10каждойстороны
в обоихсценариях,base+stressnet>0,оба2025Hstress>0,stressбезлучшей>0,DD<=2%.
ВыбратьОДИН поstressnet→stresscount→lexicalasset/TF/family. Опубликоватьвыбор
до2026,проверитьтольковыбранныйтемижеусловиями2026H1/H2;никакихrunnerup,
переворота/порога/сменыhold после результата. Успех=исследовательскийкандидат,
не разрешение рабочих сделок. Еслиnone,не считать2026для проигравших.

Проверки:closed-onlyNR7,возрастстарогоэкстремумабезтекущей,dual/conflict,
sourceprefix,gap/stopdistanceдофинансов,собственныеmean/trendвыходы,
time/stop/funding/капитал/step и независимыйаудит. Прежние210sourcehashes,
нет новых данных/сервисов/платных запросов/VM/бюджета/credentialдоступа.
