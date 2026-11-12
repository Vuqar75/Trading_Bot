# Цена после сокращения OI: фиксированное правило и ценовой контроль

Продолжениепоиска, не исправление завершённых отрицательных backtests.
Признакновыйдлядетерминированногоправила: sum_open_interest в натуральных
единицах, НЕ sum_open_interest_value(USDT),которыймеханическименяетсясценой.
СнижениеOI не доказывает ликвидацию или направлениеумныхденег. Проверяется
реакцияценыпослесокращенияконтрактов,а не заявляется причиннаяинтерпретация.

Сохранённый metricscache:2025-06-30..2026-10-31. Поэтомуdevelopmentстрого
2025-07-01..2026-01-01; не придумывать2025H1. Две разновидности на каждом
BTC/ETH/SOL: price_control и oi_filter, один15mTF. Всего6вариантов×2cost.
Обе используют одинаковыеavailablemetricsмаски длясопоставимости.

Setup=s предыдущая15mсвеча. |C_s−O_s|>=1.5ATR14Wilder,избаровДОsetup.
Если C_s<O_s, LONGтолько еслиследующаяconfirmationсвеча зелёная иC>C_s;
еслиC_s>O_s, SHORTпри краснойconfirmation иC<C_s. Решение T послеCLOSE
confirmation. OIисточникдляsetupend=T−15min: последнийsnapshot<=этойграницы,
свежесть<=5min; сравнитьспоследнимsnapshot<=setupend−1h(тоже<=5minfresh).
ЗначитдаженовейшийOIдлярешениястарше15min. Публикационныереальныелаги
историческимиархиваминедоказаны; это консервативный сценарий доступности.
oi_filterтребует OI_after/OI_before−1<=−0.01; price_controlигнорируетизменение,
нонеобходитотсутствиеисточника. НиRSI,ни60%MLgate,ниобучениездесьнеиспользуются.

Anchor=EMA20закрытийДОsetup, зафиксирован ДОшока; stop=min(lowsetup,
lowconfirmation)дляLONG, maxhighобеихдляSHORT. Отсигнальнойценыдоanchor
>=0.6%; stopdistance>0и<=2%. Дополнительно наreferenceclose соstresscost
fee5/impact10bps/side условный net_at_anchor>=|net_at_stop| и net_at_anchor>0.
Это геометриясценария,не вероятностьприбыльногоисхода. Тежеусловияпроверить
последействительноймодельнойminuteopen+impact, ДОfees/открытия. Нельзя
знатьбудущийanchor/stop,менятьего подценуилиполучатьfillнараннейсвече.

Вход T+1minбаза/T+6minстресс. ВыходпослеCLOSED15mclose достигзафиксированного
anchor,затем1/6mindelay;max6h,stopknownboundaryсgap/mark/funding,старый
финансовыйAccount. BTC0.001<=150research,ETH/SOL<=100,1x,5risk,1000USDT
насчёт.Нетproductioncapизменений,новогоAPI/данных/fit/сервиса.

Developmentдлякаждогоактива/variant:>=30закрытых/10LONG10SHORTвобоихcost,
base/stresspositive,обаQ32025/Q42025stresspositive,безлучшейstress>0,DD<=2%.
ВыбратьОДИНпоstressnet→n→lexicalasset/variant,опубликоватьДО2026. Далее
Jan-Oct2026тольковыбранный,тежеусловия(оба2026H1/H2positive). Нетrunner-up,
сниженияOIdrop/ATR/геометриипослерезультатов. Всеcontrolрезультатыраскрыть,
еслиprice_controlлучше—неприписыватьпреимуществоOI. Историяранееиспользована,
многосемействпробовано,положительностьнеявляетсяindependentalphaилидопуском.

Проверки:causalprices/anchor/sourceOIlag,контрактывместоUSD,missingне0,
knownstop/gapgeometry,pricecontrolсобщейдоступностью,финансовыйаудит.
Сохранятьcheckpointкаждого завершённого счёта сinputfingerprint иhash,
чтобыпрерывание квотойневызывало повторного расчёта. Проверенные файлы
должны переиспользоваться; повреждённые не затирать автоматически.
