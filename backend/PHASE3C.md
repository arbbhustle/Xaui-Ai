# Phase 3C: causal Adaptive Meta-Council

Local DEMO companion built above immutable Phase 3B commit
`6ec0099057eac4134abe3248900499b8f3db9a0a`. All Phase 3C work is additive.
The champion source hash is checked at startup. Existing entry points, backend
URL, Android, provider adapters and champion scoring/execution remain unchanged.

## Champion versus challenger

| Property | Phase 3B champion | Phase 3C adaptive challenger |
|---|---|---|
| Production DEMO routing | Always active | Never selected automatically |
| Decision | Original technical, macro/news and hidden-state decision | Separately recorded adaptive decision |
| Learning | Original Phase 2/3A/3B behavior | Prior closed champion, challenger and three shadow outcomes |
| Positions | Original `trades` and events | Dedicated `meta_positions` and events |
| Calibration | Original gross-R target | Positive net simulated R; explicit champion proxy until own evidence exists |
| Failure | Existing risk controls | NO_TRADE and zero adaptive influence; champion continues unchanged |

The challenger cannot reverse the champion, override its veto, alter its stops,
write its positions, or feed its calibration. Even a promotion-eligible report
does not change routing. Hypothetical challenger positions have at most one active
position, next full 1m open execution, the champion's entry/SL/TP2 plan, conservative
stop-first same-bar ambiguity and adverse stop gaps. Costs affect analytics only.
Missing 1m continuity or changed market provenance pauses hypothetical monitoring;
later entry vetoes do not erase already-filled outcomes. Scheduled session gaps
follow the existing session rules. No broker interface is present.

## Data flow and causal history

1. Unchanged Phase 3B collects/validates data and persists its decision and shadows.
2. The companion monitors its own positions and imports CLOSED original outcomes.
   Original event/snapshot records supply the first-known time; a late-discovered
   close is not treated as known at its earlier economic close time.
3. A model namespace includes champion model identity, companion source, settings
   and cost assumptions. Different test permissions, code or costs cannot pool.
4. Training requires close and known time strictly before the earliest input
   candle in the actual last-120 closed bars of every timeframe, minus the default
   one-hour embargo. This conservative 4H footprint can require over 20 days of
   separation. The current outcome, future labels and open positions are excluded.
5. A separate recent, one-hour-embargoed window detects deterioration only. It can
   reduce trust, never increase training evidence or confidence.
6. Exact six-dimensional entry regime determines trust: hidden state, session,
   volatility, macro regime, timeframe alignment and BUY/SELL. Entry labels are
   captured at decision time, never relabeled from the eventual outcome.
7. Save the entire bounded evidence set, IDs, weights, policies, current champion,
   calibration inputs, uncertainty and prior transition memory for exact replay.

Temporal filters run before bounded history selection so recent excluded records
cannot crowd out older learning evidence. Each window defaults to 2,000 records
(maximum configurable 10,000); the union can contain twice that number. Greedy
earliest-close nonoverlapping intervals define effective sample size. Adjacent
exposures sharing an endpoint are conservatively not counted independently.
Timestamps are offset-aware and chronological sorting uses their UTC instants.

`walk_forward` accepts strictly increasing decision timestamps and frozen policy;
each fold uses the same causal selection, purge, embargo and prior state updates.
There is no random split, retrospective parameter search or full-history fit.
Runtime calibration metrics score probabilities actually saved before entry.

## Trust, weights, uncertainty and calibration

Baseline weights sum to one:

| Component | Weight |
|---|---:|
| Technical | .22 |
| Momentum / market structure | .12 each |
| Volatility | .06 |
| Multi-timeframe alignment | .14 |
| Macro / news | .08 / .04 |
| Gold resilience / DGFE | .04 each |
| Liquidity / entropy | .03 each |
| Event absorption | .02 |
| Each of three existing shadows | .02 each |

Missing component evidence stays null; it reduces coverage. Shadow votes require
their own minimum 30 independent, exact-regime closed samples. Other component
credit uses only actual champion/challenger trades that the component supported
by at least 20 signed index points. It does not invent opposite-trade returns.

Trust is a hit-frequency index shrunk by 20 neutral pseudo-observations. Below
30 samples, strength is zero. Nonpositive net expectancy cannot earn positive
strength, even with a high hit rate. Target weight is baseline times
`1 + 0.5 * strength`. Projection enforces sum one, hard bounds 0..0.35 and maximum
change .005 per new decision in that regime. Weak evidence/drift returns targets
toward baseline at the same bounded speed. Corrupt weights cause zero influence
and baseline reset. These are fixed model assumptions, not optimized parameters.

Disagreement is cancellation of weighted signed evidence; uncertainty is the
maximum of sample deficit, disagreement and missing coverage. Adaptive influence
is capped at .25 and reduced by uncertainty and the reliability of prior state
transitions. High uncertainty (default >=70), coverage below .7, opposing direction
or raw score below 68 vetoes the challenger. Raw/quality/trust scores are indices,
not probabilities. Proposed influence is separately retained when a later veto
sets effective influence to zero.

Calibration uses at least 60 independent prior outcomes in the same direction
and 20-point raw-score bin, with Beta(2,2) shrinkage. Exact regime calibration is
used only with enough samples; otherwise it pools that direction/score bin.
Actual challenger outcomes are preferred; otherwise the source explicitly says
`CHAMPION_PROXY`. No estimate is supplied below minimum sample count. Minimum
eligible estimated confidence is .55. A champion proxy is **not validated
challenger confidence**, and a historical estimate is not a guaranteed win rate.

State-transition frequencies use prior counts only; the current transition is
added to memory for the next decision. Default drift compares 20 recent outcomes
with 60 preceding outcomes, by each strategy and exact regime. Negative recent
expectancy plus a drop of at least .5R triggers fallback. A drift flag in any
cohort conservatively blocks adaptive action, including outside that cohort.

## DEMO cost assumptions and evaluation

Defaults are quote-price points, not broker pips or observed executable costs:

- Round-trip spread: **0.30 XAU/USD price points**.
- Slippage: **0.10 points per side**, total 0.20.
- Additional latency penalty: **0.00 points**, configurable.
- `net_R = gross_R - (spread + 2 * slippage + latency) / initial_risk_points`.

Default total deduction is 0.50 points divided by initial risk. For a 2-point
stop distance, +1 gross R becomes +0.75 net simulated R. Fees, financing, variable
spread and real latency paths are not modeled. These assumptions are configurable
through `DemoCosts`, recorded per outcome and isolated by namespace. They never
rewrite original gross outcomes or pretend to be live broker measurements.

Analytics separate champion, challenger and all three shadows. They expose gross
and net expectancy/totals, hit rate, sample count, effective sample size, closed
equity drawdown, Brier score, calibration error and reliability bins. Original
champion confidence is evaluated against its original gross target; challenger
confidence is evaluated against the net target. Only predictions strictly before
entry count. Per-dimension learning diagnostics retain sample count/expectancy.

## Human-only promotion gate

Only `PROMOTION_INELIGIBLE` or `PROMOTION_ELIGIBLE_FOR_HUMAN_REVIEW` is returned.
Eligibility requires at least 100 independent actual challenger outcomes, positive
net expectancy, drawdown <=8R, >=60 recorded pre-entry calibrated outcomes,
Brier <=.25, calibration error <=.10, >=3 states and >=2 sessions, >=4 weeks with
no week exceeding half the sample, and >=20 positive-expectancy samples in each
of three states. The last 25 outcomes must also have positive expectancy.
Integrity/leakage flags, replay mismatch, active drift or current companion
fallback block eligibility. Synthetic test successes do not establish operational
eligibility. There is no promotion endpoint or automatic champion replacement.

## Persistence, replay and failure containment

New tables: `meta_decisions`, `meta_outcomes`, `meta_positions`,
`meta_position_events`, `meta_control`. Champion decisions and all replay inputs
are embedded in checksummed meta records. Outcome imports verify original decision
and snapshot integrity and agreement between closing event and final position.
Replay reruns the pure evaluator with stored history, never today's provider data.
Source hashing normalizes line endings. Keep the original release for old replay.

Savepoints isolate companion collection/execution and decision failures from
champion writes. Logs contain generic codes, not raw provider/exception contents.
A detected replay mismatch creates a persistent companion block; no automatic
reset bypass exists. API responses apply current market/macro/slow freshness,
heartbeat, expiry and latest companion health guards without rewriting historical
decisions. Returned decisions are explicitly historical records.

Logical isolation still shares process, disk and database infrastructure within
the new local service. Disk exhaustion or SQLite failure can affect that service.
Checksums detect accidental corruption; they are not signatures against an
attacker able to rewrite records and hashes. Retention must preserve audit inputs.

## Local entry point

```powershell
python -m uvicorn backend.phase3c_main:app --host 127.0.0.1 --port 8004 --workers 1
python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short
```

Default DB is `backend/data/phase3c.sqlite3`, configurable with
`PHASE3C_DEMO_DB_PATH`. Startup rejects Render, configured earlier-phase paths and
populated original databases without the companion schema. Use a separate local
database. Importing the app creates its local schema; it does not start trading.

Existing `/signal` remains champion-only. Companion endpoints:
`/meta/decision`, `/meta/analytics`, `/meta/promotion`, `/meta/replay/{decision_id}`.
Replay can persist a safety block when corruption is found. No public deployment
or authentication changes are supplied. The existing workflow already discovers
all backend tests, so no immutable workflow/dependency change is needed.

## Limits and prerequisites

No statistical procedure guarantees absence of overfitting. This implementation
prevents the tested timestamp/outcome leakage paths and constrains adaptation;
it still needs untouched point-in-time validation and long forward DEMO evidence.
Component credit is observational and selection-biased, not proof of causation.
Correlated brains and exact-regime sparsity can make warm-up very long. No real
historical training corpus or operational promotion evidence was fabricated.

Providers remain those required by Phase 3A/3B: entitled XAU/USD access (existing
`TWELVE_DATA_API_KEY` interface), DXY/USD, intraday 2Y/10Y yields, point-in-time
economic calendar, financial/geopolitical news, optional Fed expectations, COT,
ETF flows, physical context and options/skew/crowding. No new credential is needed
for this offline layer. Provider selection determines additional keys/licensing;
public COT may require none. Missing inputs remain unavailable, never synthetic
live fallbacks. TEST_DATA policies and provenance remain enforced by Phase 3B.

**Pre-deployment storage optimization remains open: about 76 MiB per 1,440 ticks
was already measured before this layer.** Phase 3C additionally repeats bounded
historical evidence in replay records. Analytics scan outcomes in memory and
imports recheck a bounded 10,000 source events per cohort; neither is a large-scale
storage solution. Plan compaction/content-addressed evidence, archival, backup/
restore, bounded analytics and access controls before operational exposure,
without destroying replay history. No database was deleted or migrated for this work.
