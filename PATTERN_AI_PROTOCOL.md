# Frozen technical-pattern AI experiment

Owner explicitly requested AI technical/pattern analysis. Existing GPT executor
classifies a4h regime from a small numeric snapshot; it does not learn whether
a candlestick setup survives costs. This experiment adds genuine trained ML
and a structured GPT technical-analysis packet without new paid calls.

Different question from the rejected hourly XGBoost return regression: predict
the probability AND expected stress-net return of a closed15m technical setup.
No repeat of old training runs or frozen RSI parameter search.

UniverseBTC/ETH/SOLUSD-M,existing210verified archives,Dec2024warmup.
Closed15m candle patterns: directional engulfing,pinbar,inside-range breakout,
20bar range breakout. Both directions; contradictory direction flags skip.
Features:candle anatomy,RSI14,ATR14,EMA9/20/50/200distances,ADX14,volume ratio,
returns1/4/16/96bars,BBwidth,closed1hEMA20/50and4hEMA20context,hourcyclic,
pattern flags,sideandasset. No chart-image guess or future peak confirmation.

Counterfactual label for each candidate: immediate protected trade after1min
base/6minstress,stop clip(1.5ATR%,0.5%,2%),target2R,timedexit4hfromentry.
Adverse stop before target on same minute,stopgap at adverseopen. Actualfunding,
fees5bp/side,impact5/10bp/side. BTCfixed.001<=150,ETH/SOL<=100,1x,risk<=5.
Labels can overlap because they are training counterfactuals,not actual trades.
Execution comparison permits only one position per asset/scenario and never
uses future outcome to select an entry. Only complete4hlabel windows admitted.

Pooled6month rolling train,nextmonthtest,July2025-Oct2026,16folds. Purge any
training label whose maximum outcome end(signal+6min+4h)reaches testboundary.
No test labels used forfit/scaling/threshold. Forest classifier and regressor:
64trees,depth5,minleaf40,seed42,njobs2;unweighted classes. Fixedentrygate:
p(win stress)>=0.60 AND predictedstressnet>=5bp. No gridsearch,thresholdtuning,
featureselection or extraassets after results. Fewer than200trainexamples or
one class means NO_TRADE. Probabilities are uncalibrated estimates,not guarantees.
Baseline trades all identical eligible setups with identical risk/execution.

Report everyasset,base/stress,baselinevsAI,count,direction,net,costs,DD,
exitquarter and besttrade sensitivity. Economicresearchcandidate requires
>=60AIclosedtotal,>=10eachdirection,totalbase/stresspositive,stresswithoutbest
positive,>=3/4lastfourstressquarterpositive,perassetDD<=2%,no unexplained
ledger/leakage failures,andAIstressbetterthanbaseline. Multiple experiments
and reusedhistory mean this still is not independentOOS or trading admission.

Save folds,predictions,labels,ledgers,latestmodel and explainable inputpacket.
Export forest trees asJSON,not arbitrary pickle. Pure inference rejects wrong
market/schema,stale models,nonfinite ormissingfeatures. No model controls
financial DB,pauses,sizeorbudget. GPT builder binds numeric/patternpacket to
snapshot hash and uses existing gpt-6-luna name; no transport,secondbudget,
APIkeyreadorpaidrequest. Working VM unchanged; no automatic admission.
