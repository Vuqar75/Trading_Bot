# AI analysis improvement: measured experiments and factual positioning context

## Delivered change

Implemented OPTIONAL `--derivatives-context` for the existing patternAIserver.
It collects publicUSD-M open-interest history,global/top-account/top-position
ratios and taker BUY/SELL,then supplies measured facts to GPT and `/ai patterns`.
No newservice,timer,budget,credential or tradingengine. Current RFinputvector
andmodelJSON are unchanged. The newmetrics explicitly say `used_by_local_ml:
false`; no unqualified extended forest is presented as an installed upgrade.

The live collector uses the officialUSD-M endpoints documented at:
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data

OI/account/position endpoints label periodEND; taker labels periodSTART and
is normalized to its5minEND before as-of selection. Data are selected at most
signal−15min,with up to5min stale tolerance and explicit source timestamps.
OIpercentchanges use OIunits,not USDnotional growth caused merely by price.
Account-count and top-position ratios are different populations; OI alone
does not establishLONG/SHORTdirection,liquidation or executableliquidity.

Only5fixedmarket-dataGETendpoints are added,parallel/bounded8sectimeouts,
no key/header/private endpoint,automaticretry or alternatehost. A401/403/451
or invalid component becomes explicitpartial/unavailable;no zero-imputation,
newBinancekeyrequest or guessedmetric. The base candle path remains intact
and the option defaultsOFF. Same-barcache handles the firstoptionactivation
and avoids repeating collection eachminute. Neither option nor button can
enable paid calls or financialjobs. Existing one-paid-boundary guard remains.

Venue/symbol/cutoff,raw observationtime,periodEND,positivefinite values,
source age/conflicts and fact-source bindings are validated both before
publication and at GPTrequest construction. Stale/invalidpublisheddata stay
unconfirmed. `/ai patterns` labels data age/partial coverage and separates
factual metrics from the unchanged local ML forecast. Rootpublisher and bot
must both have the purestdlib `research.derivatives_context` validator.

## Completed experiments: no performance promotion

Two experiments were frozen before calculation,without threshold/asset/grid
search. Neither met its preregistered analysis-quality promotion criteria.
They remainoffline; working model and serverpauses/paidwindows unchanged.

### Chronological calibration

Protocol023bcc5. Perasset isotonic probability and net-return calibration
uses only previous3months'out-of-fold forecasts/finishedlabels with day-balanced
weights. Oct2025-Oct2026:40,861matchedtestforecasts. No RFretraining.

| Metric on identical cases | Original | Calibrated | Relative change |
|---|---:|---:|---:|
| Net-returnMSE | 0.00006625936 | 0.00006698080 | +1.09%error |
| Brier score | 0.19533971 | 0.19627313 | +0.48%error |

The new uncertainty/support researchgate also yielded0candidates. Neither
zeroentries nor better-looking probabilities is called a profitable strategy.
`analysis_promotion_eligible=false`; no calibration file is wired into live
inference. No currentmodel gate was lowered.

### Derivatives-feature forest comparison

Protocol0f7ab7d,feature-code74eb91c,inputmatrix/source pin39b88a7.
1,467daily officialarchives,allSHA256checked,489days perBTC/ETH/SOL.
Inputroworder was not chronological; timestamps were sorted before lookup.
Invalid/nonpositive rows and stale lookups were excluded,not fabricated.
Trainingexample coverage:BTC99.913%,ETH99.899%,SOL99.861%. Historicalpublication
and revisiontime areunknown;15mlag doesnot prove truepastavailability.

Appended one fixedsetof10positioningfeatures to34existingfeatures with the
same64tree/depth5/minleaf40models. Jan-Oct2026:31,233matchingtestforecasts,
6monthpurgedtrainpermonth. Cached original predictions reused. Newtraining
lost55examples inJanand7inFebbecause ofsourcecoverage;Mar-Octtrainingcounts
match exactly. Thus small scorechanges are not a clean causal attribution to
features alone. Every comparison uses identicaltestcases.

| Metric | Original | With positioning | Relative change |
|---|---:|---:|---:|
| Net-returnMSE | 0.00005994484 | 0.00005998849 | +0.073%error |
| Brier score | 0.19264794 | 0.19282860 | +0.094%error |

Bothpolicies produced0candidates under the original60%/5bpgate. Positive
regressorpredictions selected106old/123newoverlappingcounterfactuals; actual
meanstressnet was−52.39/−68.13bp,respectively. This is NOT portfolioPnL or
proof of statisticalsignificance. Extendedmodel was not exported/promoted.
Inputs are reusable validated structuraldata,not evidence of profitablealpha.

## Verification and operational limits

89targetedchecks passed:as-of/futurelabels,archiveorder/conflicts,calibration
support/modelbinding,takerstart/end,partial/unavailableinputs,no fabricated
metrics,noeffectonoldMLinput,read-onlyCLIwithoutpaidtick,GPTbinding,legacy
budget path,existingpaid-boundaryguard and Telegram. Fullapp/oldbacktests were
not repeated. No standalonefinancialreplay was needed with0newcandidates.

Audited all50,573available metricfeature rows: eachlag uses a timestamp before
the signal,within5minof itsdelayedtarget. Source/featurematrixpinnedbeforefit.
Reports,folds,predictions,feature receipts,comparisonaudit and historicalUI
example are retained under `pattern_calibration_results` and
`derivative_feature_results`. Examples are explicitlyhistorical,not freshVMdata.

Cloud has no VMSSH/GCPaccess; the previously reportedFAPI451restriction was
not retried or workedaround. No currentRESTmetricavailability or server
installation is claimed here. No paidGPTcall,APIbudget/credentialaccess,
workingDBchange,pausechange,newresearchservice or VMrestart. Localdeployment
instructions:DERIVATIVES_CONTEXT_LOCAL_TASK.md. The existingmodel remains an
unqualified researchmodel; profitablefinancialentries remainunconfirmed.
