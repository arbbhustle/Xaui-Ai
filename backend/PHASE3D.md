# Phase 3D: offline validation and research lab

Base: committed Phase 3C `0bbfe2e162857064eae562dfe243521cb992fd1d`.
All implementation is additive under `backend/research`. Existing engines,
services, Android, Render configuration, champion routing and promotion code are
unchanged. No network client, broker, notification or deployment action is added.

## Research architecture

```
as-received capture archive + provider attestation
  -> immutable manifest / append-only experiment registry
  -> chronological warm-up / training / validation / sealed holdout
  -> independent temporary model databases
  -> unchanged model evaluators + explicit research adapters
  -> native outcomes / separate execution-only counterfactuals
  -> cost accounting / uncertainty / cohorts / robustness / scorecard
  -> evidence status (never routing or trading authorization)
```

Models: Phase 2, Phase 3A, Phase 3B, Phase 3C, NO_TRADE, EMA(8/21), RSI(30/70),
20-bar breakout and single-bar 5m trend. Phase 3B's three existing shadow variants
are reported separately. Baselines have fixed rules, no invented probabilities,
1.25 ATR / minimum .0008 price risk and 2R targets; they retain data, session,
kill-switch and native position/episode controls. They intentionally omit council
score/bias gates. No threshold is chosen to produce a 75% win rate.

The original evaluators run inside research-only subclasses with isolated state.
Research adds **warm-up-only vetoes and calibration purge/embargo**; therefore the
research results must not be misrepresented as unmodified production replays.
Warm-up collects features, news identity and prior macro context without creating
champion, challenger or shadow outcome labels. Training then creates chronological
DEMO history. Validation/holdout keep the same frozen algorithm; online learning
can use only sufficiently old closed outcomes, never future results.

## Input contract

Supply JSON with:

```json
{
  "schema": "as-received-captures-v1",
  "dataset_id": "stable-governed-dataset-name",
  "provider": "archive-provider-name",
  "mode": "HISTORICAL",
  "point_in_time_attested": true,
  "ticks": [
    {
      "at": "2026-01-05T14:00:10+00:00",
      "captured_at": "2026-01-05T14:00:10+00:00",
      "frames": {},
      "source_errors": {}
    }
  ]
}
```

The empty `frames` above illustrates the container only. Real frames must contain
the existing five timeframe candle arrays and Phase 3A/3B envelopes:
`__market_provenance__`, `__intelligence__`, optional `__slow_regime__`.
Reuse their documented schemas. Provide actual archived receipt/publication times,
not downloaded final revised series relabeled as historical observations.

Captures must be strictly increasing, offset-aware, and captured at the decision
instant. Candles must already be fully closed. Future source observations,
publications, retrievals and revision receipts are rejected. Future scheduled
calendar events are allowed when the schedule was already known. A changed source
ID requires a new explicit `revision_received_at` later than the old version's
first capture. Earlier captures remain immutable. Revised OHLC history requires a
separate dataset; it cannot silently rewrite fills.

Provider ordering is normalized; duplicates remain visible and flagged. Malformed,
duplicate, missing and stale candles are flagged; source integrity/risk vetoes
remain visible in each report. Integrity flags block evidence qualification.
TEST_DATA requires explicit opt-in, remains labelled synthetic, and cannot yield
promotion evidence. Synthetic envelope/record markers in HISTORICAL input are
rejected. Unavailable inputs are never replaced with invented observations.

An honest provider/archivist is a trust boundary. Software cannot prove that an
attestation or all supplied timestamps are truthful. Revised data with stripped
version history is not acceptable research evidence, even if its JSON is valid.
Credential fields and credential-bearing source URLs are rejected before archival.

## Splits, causal learning and final holdout

`Split` requires strictly increasing `warmup_start`, `train_start`,
`validation_start`, `holdout_start`, `end`, plus `embargo_seconds` (default 3600).
All ranges are half-open. Validation never sends holdout captures to validators,
feature evaluators, reference classifiers or outcome simulators. Full archive
hashing/freezing is permitted, but does not supply future information to models.

Calibration labels must close and become known strictly before the earliest
closed input candle of the feature window minus embargo. The 4H 120-bar footprint
can require roughly 20+ days of separation. Phase 3C additionally retains its
existing conservative history rules and at least one-hour embargo. Recent labels
can only reduce adaptive action through the existing drift guard. No open or
same-decision outcome trains a current prediction. Cross-boundary trades are
excluded from fold outcome statistics, not artificially liquidated; censored/open
positions and observed exposure are explicitly reported.

Warm-up/training are never scored as validation. No random temporal split exists.
The final holdout requires a previously completed validation with exactly the
same data hash, code hash, model/scenario list, settings, seed, costs and split.
The first holdout attempt locks the **whole dataset**, even if that run fails.
Further tuning or another holdout request is rejected and retained in the registry.
Replay permits only the same completed immutable experiment, not altered settings.

These controls govern one maintained registry and stable dataset ID. Renaming a
dataset, deleting the registry or starting another lab can bypass local governance;
use a centrally retained registry and reviewer controls for real research. No code
can prevent a researcher from manually learning from already-seen holdout values.

## Ablations and parameter sensitivity

Each ablation is anchored to the Phase 3B research adapter. The adaptive-meta
ablation compares Phase 3C with Phase 3B. Function globals are copied into isolated
functions; shared module globals and production source are never patched.

| Removed component | Exact research intervention |
|---|---|
| Technical, momentum, structure, volatility, multi-timeframe | Zero that council component's weight; renormalize remaining weights |
| Macro | Remove USD/yield/macro blending and their signed contribution to downstream pressure |
| News | Remove news blending and signed contribution to downstream pressure |
| Resilience | Remove residual score/components and resulting pressure contribution |
| DGFE | Remove fracture, expansion-candidate and directional-pressure output/state paths and hidden conflict gate |
| Liquidity | Remove sweep, reclaim, rejection, displacement, follow-through and signed pressure paths |
| Entropy | Remove entropy score/veto; retain path efficiency used independently in state classification |
| Event absorption | Remove post-event absorption reports/shock path; retain calendar blackouts and independent market shocks |
| Slow regime | Remove optional background assessment/veto in this research variant |
| Adaptive meta-council | Use the Phase 3B research decision instead of the isolated adaptive decision |

These are **specified contribution ablations**, not claims that every correlated
feature or safety gate associated with a name has disappeared. Timeframe data
validation/disagreement gates, macro/news freshness and critical calendar safety
are retained even when score contribution is removed. This avoids confusing
removal of information value with removal of basic data safety.

Comparisons report net expectancy, drawdown and calibration differences. Fewer
than 100 outcomes per comparison yields INSUFFICIENT_EVIDENCE. Bootstrap interval
separation is descriptive, not a multiple-testing-adjusted significance claim.
No component is automatically kept, removed or declared a winner.

Sensitivity runs nearby entropy thresholds 75/78/81 by default, on validation only.
Other valid hidden settings can be specified programmatically. All variants remain
in the ledger; no selected/best value is returned. Sign changes or >.5R expectancy
range are flagged as fragile, with low-sample status taking precedence in reporting.

## Metrics, costs and robustness

Per model/cohort: trade count, win/loss/breakeven rates, gross/net R, expectancy,
average winner/loser, payoff, profit factor, drawdown, winning/losing streaks,
recovery, signals/day, observed exposure and NO_TRADE percentage. Undefined ratios
remain null. Open positions never masquerade as completed outcomes.

Cohorts use contemporaneous independent Phase 3B reference classification for
all compared models: all eight hidden states; Asia/London/overlap/New York;
BUY/SELL; ATR-ratio volatility HIGH >=1.5, LOW <.75, otherwise NORMAL; macro
supportive/adverse/mixed; aligned/mixed/opposed timeframes. Unavailable and other
observed labels remain visible. The reference classifier never changes entries.
Sessions retain the existing engine's fixed UTC windows, not DST-adjusted sessions.

Costs default to .30 price points spread, .10 slippage per side, zero latency
penalty: `net_R = gross_R - (.30 + 2*.10) / initial_risk_points`.
They are simulated assumptions, not broker measurements. Financing and commission
are absent. Wide spread and high slippage each multiply the applicable term by 3.

Other scenarios: 1m/2m delayed entry, adverse .50-point entry drift, deterministic
missed candles, stale macro, 30-minute withheld news, missing DXY/yields and periodic
provider outage. Data stresses rerun native models. Execution stresses replay the
same recorded signals through a separate counterfactual executor; they **do not
feed altered fills into model training**. The same distinction applies to shadow
stress outputs. Stop wins intrabar ambiguity; gaps and missing continuity remain
conservative. Costs are deducted exactly once after gross execution.

Brier/ECE/reliability bins score only probabilities recorded strictly before entry.
Champion probabilities retain their gross target; Phase 3C retains its net target.
Over/underconfidence are weighted calibration-bin gaps, not individual forecast
errors. Missing confidence remains missing; raw scores are never probabilities.

The 75% target is evaluated only after outcomes: achieved in sample, sample size,
Wilson 95% interval, net expectancy and validation/holdout qualification are shown.
There is no target-driven optimization. Wilson intervals assume Bernoulli sampling;
dependence-aware block-bootstrap intervals are also reported.

Seeded circular block bootstrap (default seed 173, 300 replicates, five trades/block)
reports expectancy, profit factor where defined, ECE, win rate, drawdown and losing
streak distributions, plus probability of drawdown above 125% of observed and
nonpositive expectancy. It preserves local sequence dependence approximately, not
all long-range/calendar dependence. It is not a forecast guarantee. Cohorts below
100 trades are flagged. Monthly stability exposes degradation, sign instability,
insufficient periods and one-period dominance.

## Frozen manifests, registry, archive and replay

Each manifest includes code/model identity, immutable base commit, complete data
hash/provenance, actual capture range, split ranges, default policies and overrides,
thresholds, cost assumptions, models/scenarios and seed. A hash-addressed manifest
cannot be overwritten. SQLite registry entries are append-only with a checked hash
chain, no-update/delete triggers and completed/failed/rejected attempts retained.
Invalid completion and duplicate terminal states are rejected. Connections close
explicitly, including on Windows. Errors retain only exception type, never raw
provider messages or credential text.

Reports count all attempted variants and validation reuse. No favorable-regime
filter or winner selector exists. This plus frozen holdout controls reduces
cherry-picking; it does not eliminate researcher discretion or multiple testing.

Archived documents are compressed/hash-verified; large containers are chunked.
Decompression/object/depth bounds fail closed. `reproduce` reconstructs captures,
verifies data/code/prefix identities, reruns isolated engines, and compares exact
results. Retain original source releases for old experiments. The separate storage
prototype additionally deduplicates immutable candle/source records and stores
references rather than repeated records. Nothing migrates operational databases.

## Local use

```powershell
python -m backend.research run --data captures.json --split split.json --lab research-lab
python -m backend.research run --data captures.json --split split.json --lab research-lab --all-ablations --all-stresses
python -m backend.research sensitivity --data captures.json --split split.json --lab sensitivity-lab
python -m backend.research run --data captures.json --split split.json --lab research-lab --stage holdout
python -m backend.research replay --lab research-lab --attempt 1
python -m backend.research profile --data captures.json --lab storage-lab
python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short
```

Use an external/disposable lab directory, never an operational database directory.
`--allow-test-data` is required for explicitly synthetic archives. Select exactly
the same frozen variant list for validation and holdout. Sensitivity has no holdout
mode. Importing the research package performs no I/O or service startup.

## Evidence status and limits

Outputs use INSUFFICIENT_EVIDENCE, RESEARCH_VALIDATION_PASSED or
FORWARD_DEMO_REQUIRED. ELIGIBLE_FOR_HUMAN_REVIEW is reserved for a future separately
reviewed forward-evidence process; this historical-only lab cannot emit it.
Qualification is conservative: enough positive-net champion/challenger samples,
drawdown/calibration limits, positive lower bootstrap expectancy, period/regime/
session breadth, clean integrity, no unresolved positions and all stress results.
Historical results never authorize trading or change the original promotion gate.

No real historical corpus was supplied. Required inputs remain point-in-time
XAU/USD, USD/DXY, 2Y/10Y yields, calendar releases/revisions, news editions and receipt
times, optional Fed and COT/ETF/physical/options context. Credentials/licensing
depend on providers; no new credentials are required by the offline harness.
The existing Twelve Data interface alone is not proof of complete historical
macro/news archives. Final-value-only downloads cannot validate this system.

Remaining operational work: representative historical/holdout and forward DEMO
evidence, independently reviewed assumptions, provider attestations, governance,
storage capacity/backup/access controls and scale testing. Current analysis loads
corpora/reports into memory and runs models serially; it is not a distributed
historical-data platform. See PHASE3D_REVIEW.md and PHASE3D_SCORECARD.md for measured
storage findings and the deliberately inconclusive fixture scorecard.
