# Single structural-input extension for the existing pattern AI

Registered before computing extended features/model results. The preceding
isotonic calibration failed its promotion gate; it will not be installed.

Hypothesis: derivatives positioning contains information absent from OHLCV
pattern features. Append exactly10features to the frozen34,using official
Binance USD-M daily5m metrics: log OI change1h/4h/24h;log top-account,
top-position,global-account,taker ratios;top-position/global-account contrast;
4hchange global-account ratio;side*global-account logratio. These are
positioning proxies,not proof of forced liquidation or executable orders.

Source availability/schema checked ononeofficial2026-10-31BTCarchive;rawrow
order is not chronological. Sort by time,verify each officialSHA256,UTCdate,
5malignment,symbol,positivefinite required values,duplicates/conflicts. No
interpolation. For signalT,use only metrics timestamp<=T−15m;lag features use
corresponding earlier as-of timestamps. Reject samples if any lookup is stale
by>5m. Historicalpublication/revisiontime is unknown;15mlag is conservative
but doesnot certify actualpastavailability. Require>=99%matching example
coverage perasset before training;report gaps,do not hide source problems.

Needed metricdays2025-06-30..2026-10-31inclusive,3fixedassetsBTC/ETH/SOL.
Localpinnedcache;no paid API,no failedFAPIendpoint/proxyworkaround. New input
matrix and manifests pinned before results. Existing exact target/costs and
originalforecasts are reused. Evaluate Jan-Oct2026(10months);fit6monthspast
pooledassetdata eachmonth,purge labelwindows past cutoff. Same64foresttrees,
depth5,minleaf40,seed42 and classifier/regressor;nohyperparameter/asset/TF
sweep. Missingtraining/testcases explicitlyexcluded;compare originalforecasts
on IDENTICAL available testrows and disclose trainingcoverage differences.

Primary analysisqualitygate: >=1%relative improvement in BOTH pooledBrier
and netMSE;noasset worsens either by>2%. Entrycriterion stays original
rawP>=.60 andpredictedstressnet>=5bp,no loweringtogettrades. Reportforecast
quality,selectedcounts/conditionaltargetnet,andoriginalpositive-tail behavior.
If entrysampleislargeandpositive,replay selectednonoverlapping trades before
any tradingadmission. Betteraverageforecastquality alone cannot authorize
financialentries. No livefeature/server/modelreplacement unlessindependent
runtime/sourcecompatibility and same-budget protections are checked.
WorkingVM,pauses,APIcredential,budget/locks and paidboundaries unchanged.
