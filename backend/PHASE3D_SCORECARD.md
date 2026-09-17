# Phase 3D research scorecard and storage findings

## Evidence status

**INSUFFICIENT_EVIDENCE.** No real point-in-time historical/provider corpus was
supplied. The runs below are explicitly TEST_DATA plumbing checks, not evidence
of profitable trading. No winner, 75% achievement, calibrated real-market
confidence or promotion eligibility is claimed.

The fixture capture spans eight five-minute observations. Its warm-up, training,
validation and holdout boundaries are separate; validation contains three decision
times. Trades crossing into validation from training are deliberately excluded
from validation outcomes. The holdout was not used to pick a variant.

## Benchmark and model scorecard

| Model / hypothesis | Validation decisions | Eligible closed outcomes | Win rate | Net expectancy |
|---|---:|---:|---|---|
| Phase 2 | 3 | 0 | unavailable | unavailable |
| Phase 3A | 3 | 0 | unavailable | unavailable |
| Phase 3B Champion | 3 | 0 | unavailable | unavailable |
| Phase 3C Challenger | 3 | 0 | unavailable | unavailable |
| TREND_CONTINUATION shadow | 3 | 0 | unavailable | unavailable |
| SWEEP_RECLAIM shadow | 3 | 0 | unavailable | unavailable |
| RESILIENCE_DIVERGENCE shadow | 3 | 0 | unavailable | unavailable |
| NO_TRADE baseline | 3 | 0 | unavailable | unavailable |
| EMA trend baseline | 3 | 0 | unavailable | unavailable |
| RSI baseline | 3 | 0 | unavailable | unavailable |
| Breakout baseline | 3 | 0 | unavailable | unavailable |
| Simple 5m trend baseline | 3 | 0 | unavailable | unavailable |

Zero eligible closures is not a zero win rate. It is an absence of evidence.
Raw/net totals of zero likewise do not establish a zero-risk or profitable model.
The harness retains censored/open positions, cross-boundary exclusions, exposure,
NO_TRADE frequency, costs and all cohort labels in its machine-readable reports.

## Ablation report

All 14 requested variants executed: technical, momentum, structure, volatility,
multi-timeframe, macro, news, resilience, DGFE, liquidity, entropy, event absorption,
slow regime and adaptive meta-council. Each produced three validation decisions
and zero eligible closed outcomes in this short fixture. Every comparison reports
**INSUFFICIENT_EVIDENCE**. No claim of added value, reduced drawdown or improved
calibration can be made from this sample. The 23-model benchmark/ablation run
retains every variant; none is selected as best.

See PHASE3D.md for the exact intervention in each ablation. Contribution ablations
retain underlying data-safety gates; they are not unrestricted deletion of every
correlated signal bearing the same name.

## Robustness and sensitivity

All 12 scenarios are exercised for both Phase 3B and Phase 3C: base, wider spread,
higher slippage, 1m/2m delay, missed candles, stale macro, delayed news, missing
DXY, missing yields, temporary outage and entry drift. That is 24 model/scenario
evaluations, with Phase 3B shadow stress outputs also kept separately.

This fixture supplies no eligible validation outcomes, so **where economic edge
disappears remains unknown**. Missing/stale/conflicting input tests verify visible
integrity flags and safety behavior, not profitable robustness. Execution-delay
results are explicitly signal-fixed counterfactuals without learning feedback.

Nearby-threshold sensitivity uses validation only, records all attempts, and
returns no selected value. Its low-sample output cannot establish parameter
stability. A real multi-regime chronological corpus is still required.

## Calibration and 3-out-of-4 target

Real calibration, Brier score and 75% target achievement remain unavailable. A
deterministic arithmetic regression with 100 fabricated outcomes, 75 wins and
predictions of .75 gives Brier .1875, ECE 0, and zero calibration-bin overconfidence/
underconfidence. This checks the formula only. It is **not a trading experiment**.

Actual research reports retain pre-entry probability timestamps, gross/net target
definitions, Wilson intervals, dependence-aware bootstrap intervals, low-sample
warnings and out-of-sample status. Raw strategy scores are never treated as
probabilities. No threshold or period was adjusted to manufacture a 75% win rate.

## Storage profile

Measured in a disposable Phase 3C research database using **60 TEST_DATA captures
at one-minute intervals**, with training enabled from the first measured capture.
All archived snapshot round trips were checked for exact equality.

| Measurement | 60-capture sample | Linear estimate per 1,440 captures |
|---|---:|---:|
| Canonical snapshot payloads | 3,618,422 bytes | 82.82 MiB |
| Compressed snapshots individually | 407,515 bytes | 9.33 MiB |
| Compressed deduplicated prototype plus root references | 877,344 bytes | 20.08 MiB |

The prototype contains 2,571 immutable objects and a 4,021-byte root index.
Snapshots are dominated by repeated candle frames: 3,237,742 bytes across the
sample. Intelligence envelopes contribute 193,350 bytes and prior macro context
51,669 bytes. Smaller policy/state/identity fields and JSON syntax account for
the remainder. Counts are UTF-8 payload bytes, not physical disk allocation.

A page-level sample before final research provenance annotations used 4,608,000
SQLite bytes, about 105.47 MiB per 1,440 ticks by linear scaling. Its largest
objects were snapshots (3,674,112 bytes), meta decisions (405,504), champion
decisions (163,840), shadow events (86,016) and trade events (53,248). Metadata,
indexes, schema and state made up the rest. Small later annotation changes may
alter page allocation; this is a measured research sample, not a capacity promise.

**Whole-snapshot compression beat the fine-grained deduplication prototype on this
short corpus.** More objects/reference strings have overhead. Small-file filesystem
allocation can make the prototype substantially worse than its byte-size total.
Longer histories, real news payloads and mature meta-learning histories may change
both ratios. These estimates exclude WAL/SHM, filesystem allocation, backups and
future growth of journal/history payloads; they are not production forecasts.

The inherited **~76–78 MiB / 1,440-tick storage issue remains open before deployment**.
Nothing was migrated or deleted. The isolated archive verifies content hashes,
compression bounds, chunked large documents and deterministic reconstruction.

### Retention design, not a deployed migration

1. Keep a bounded operational tier only after defining all replay dependencies.
2. Seal immutable research manifests, decisions and capture archives first.
3. Compare whole-snapshot compressed segments with packed deduplicated segments;
   avoid assuming one-file-per-node is physically efficient.
4. Verify every reference and reconstructed snapshot before considering retention.
5. Keep checkpoint/manifest roots, source releases and immutable archive backups.
6. Test restore and full replay before any future operational pruning.

No deletion, pruning or operational retention policy is implemented by this phase.

## Evidence still required

- Real as-received XAU/USD candles and timestamped original macro/news editions,
  not final revised downloads.
- DXY/USD, 2Y/10Y yields, calendar releases/revisions, news publication/receipt,
  optional Fed context and available COT/ETF/physical/options archives.
- A governed registry, predeclared splits and fixed settings before holdout access.
- Adequate independent closed outcomes across states, sessions and calendar periods,
  with positive net expectancy and stable calibration/robustness after costs.
- Untouched holdout evidence, then separately reviewed forward DEMO evidence.

Champion remains Phase 3B. Phase 3C remains isolated and DEMO-only. Neither research
status nor historical results can promote a model or authorize live trading.
