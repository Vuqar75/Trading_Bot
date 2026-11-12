# Новый фиксированный VWAP-reentry с собственным выходом

Продолжение поиска без остановки после провала RSI2. Ровно2TF15m/1h и3
исходных актива; Jan2025 development → фиксацияодногоwinner → Jan-Oct2026
validation толькоwinner. Схема отбора,риска и раскрытия всех результатов
как в RSI2_DYNAMIC_PROTOCOL.md. RSI2параметры не меняются и не повторяются.

Источник механизма: ojeology/mean-reversion-vwap-lab, commit
5ae2853d729b6761d4be3c2ccb7602beb32665cf, прочитанный backtest_v14_honest.py.
Его MEXC20x/оптимизацияTP-SL/эмпирические blockedhours/TP-before-stop не
переносятся. Это новая Binance1xадаптация, не воспроизведение рекламногоPnL.
Чужой Python не импортируется/не исполняется. Ранее статически выявленные
ограничения источника сохранены в RSI15M_ATTRIBUTION_REPORT.md.

Расчёт по закрытымTFбарам,UTCсессия00:00, typical=(H+L+C)/3,weights=basevolume.
VWAP=sum(TP*V)/sumV; sigma=sqrt(max(0,sum(TP^2*V)/sumV−VWAP^2)). Это
стандартная weighted-moment variance, НЕ сумма отклонений от меняющегося
runningVWAP из чужого кода. Это баровый price proxy, не точный VWAP всех
реальных отдельных сделок. RSI14Wilder, ATR14simpleTR по закрытым свечам.

LONG:low<=VWAP−2sigma,close>нижнейполосы,close>open,body>0.5ATR,RSI14<40;
SHORTзеркально сupper/краснымтелом/RSI>60. Только новая transition в условие.
Сигнал действителен при расстоянии доVWAP>=0.6% против0.3%stressрасходов;
вход разрешён лишь если после1/6min и5/10bpimpact расстояние всё ещё>=0.6%.
0.6%=60bps; единицы в коде0.006, проверяются явно.

Входные часы04:00<=UTC<18:00:4hпрогревсессионныхбанд и6hmaxhold без переноса
на новуюUTCсессию. Не копировать эмпирический blacklist часов источника.
LONGвыходит после CLOSEDclose>ТЕКУЩЕГОVWAP,SHORTпослеclose<VWAP, затем
обычный1/6mindelay. Не считать touch исполнением. Stop2% внутриминутный с
гэпами, funding/ликвидация/fee5bps, impact5/10bps, всегдаmaxhold6h.
Ни2R,ни бесконечного ожидания возврата средней,ни отключения защиты.

Тот же проверенный Account из published_ema_transfer через изолированную
AST-фабрикуRSI2. BTC0.001<=150researchcap,ETH/SOL<=100,1x,риск<=5,1000USDT
на каждый отдельный актив/TF/сценарий. Рабочие caps и паузы не меняются.
Доисходные хеши включают финансовые helpers и внешний источник.

Development: все12счетов2025 раскрываются; >=30closed/10eachside в обоих
сценариях,base+stressnet>0,оба2025Hstress>0,stressбезлучшей>0,DD<=2%.
ВыбратьОДИН поstressnet→n→lexicalkey,опубликоватьдо2026. Validation2026
тольковыбранный:те же критерии с2026H1/H2. Никаких runnerup или изменения
полосы/порога/часов после результатов. Если нетdevelopmentcandidate,
2026не считать и продолжить другую гипотезу без изменения этой.
Ранее использованные данные,несколько ранее отвергнутых семей и новый
выбор из6учитываются: это не независимая alpha и не автодопуск кторговле.

Проверки:UTCreset/weightedmoments,sourceprefix,reentryLONG/SHORTпоclose,
gapdistanceдо резервирования,meanexitпоclosedVWAP,time6h,2%stop и полный
independentqueue/fill/cash/cost/funding audit. БезновойVM/платногоAPI/коллектора.
