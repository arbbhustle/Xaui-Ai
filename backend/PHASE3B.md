# Phase 3B: Gold hidden-state intelligence

Base: committed Phase 3A `f436e9c3c990d263bfdc46fae63a56eeaec1e9a5`, retaining
the Phase 2 council and Phase 1 chronological DEMO simulator. This is a separate
local DEMO service/database. No broker trading, deployment, Android changes,
notifications or Phase 3C functionality are included.

## Execution and data flow

1. Existing market/macro collectors acquire timestamped XAU and Phase 3A inputs.
2. A separate bounded collector acquires optional slow-regime observations.
3. Each feed is normalized independently; unavailable sources stay unavailable.
4. The transaction captures prior state labels, prior known macro expectations,
   source provenance, policies and calibration history in the immutable snapshot.
5. The existing council evaluates technical and macro/news scores.
6. Pure gold-state functions calculate hidden diagnostics from closed bars and
   already-known context. Slow-regime scores remain background-only.
7. Hidden/slow risk vetoes may block the council's decision. They cannot promote
   NO_TRADE to BUY/SELL or reverse a council direction. Original component scores
   and empirical calibration remain separately visible.
8. The existing simulator processes production DEMO fills/exits chronologically.
9. Separate shadow tables record three hypothetical strategy evaluations and
   outcomes. A savepoint isolates shadow failures from the main decision journal.

This is a deterministic heuristic state classifier, **not a trained hidden Markov
model or a claim to infer institutional intent**. All DGFE, resilience, entropy,
tension and slow-regime scores are indices, not probabilities. Only the inherited
historical DEMO calibration may produce a probability after its sample threshold.

## Gold features and states

The 5m closed candle is the state/entry anchor; 1m closed candles supply timing,
shock monitoring, event response and gold resilience. The inherited 15m/1H/4H
features remain cut off at the 5m anchor. Nothing uses forming candle OHLC.

| State | Rule, in precedence order |
|---|---|
| SHOCK | Current 1m or 5m true range >= 3 times its preceding baseline, or unresolved recent high-impact event |
| EXHAUSTION | Sustained move plus directional rejection wick; exhaustion index >= 70 |
| EXPANSION | Compression/displacement/follow-through index >= 65, or sufficient sustained directional efficiency/bias |
| FRACTURE | Fracture index >= 55 |
| LIQUIDITY_SWEEP | Confirmed trailing-high/low sweep or failed breakout still reclaimed inside the relevant level |
| COMPRESSION | Recent range contraction index >= 65 |
| ACCUMULATION | Low directional efficiency plus bullish liquidity support index >= 55; a proxy, not observed buying flow |
| RANGE | No stronger state evidence |

Liquidity uses trailing extrema from 20 bars **excluding** the possible sweep/
breakout and its follow-through bar. It reports upper/lower sweeps, false breakout,
reclaim direction, wick rejection, body displacement, follow-through and signed
liquidity pressure. Two-sided ambiguous sweeps stay neutral. A later breakout can
invalidate a prior sweep; the algorithm does not assume intrabar path.

DGFE exposes:

- `compression_score`: recent six-bar mean true-range contraction versus a
  preceding 20-bar baseline, clipped to 0..100.
- `prior_compression_score`: the comparable contraction before the current bar.
- `fracture_score`: 40% prior compression, 40% displacement, 20% absolute liquidity pressure.
- `directional_pressure`: 50% 5m bias, 30% liquidity pressure, 20% available gold
  resilience; signed -100..100. Missing resilience remains explicitly unavailable
  in its component and contributes no directional adjustment.
- `liquidity_pressure`: signed -100..100 sweep/reclaim or breakout pressure.
- `expansion_candidate_score`: prior compression, displacement and follow-through;
  also recognizes sustained efficient expansion without a new explosive bar.
- `exhaustion_score`: a preceding directional move plus a rejection wick of at
  least 40% of the current range, clipped to 0..100.

### Resilience

The default reaction window is 60 minutes and follows the configured macro
momentum horizon. It requires a complete contiguous sequence of closed 1m bars.
Session/data gaps or insufficient observations yield `null`, not a fabricated
neutral observation. The gold return is normalized by preceding-window range;
its signed residual against available USD/yield/macro/news pressure produces
`gold_resilience_score` in -100..100.

Positive resilience means relative upside resistance/support; negative means
relative downside weakness/resistance. Per-driver residuals separately expose
USD, yields, macro and news pressure. This is a contemporaneous heuristic
comparison, **not proof that one source caused the gold move**.

### Event absorption

For known high-impact events within the past six hours, report initial impulse
(first three completed post-event minutes), recovery time to 75% rejection,
rejection index, current follow-through in ATR units, and failed expected reaction.
Expectations are selected only from observations recorded strictly before the
event, no more than one hour earlier. Their observation time and source snapshot
ID are exposed. If no pre-event expectation was recorded, the failed-reaction
field stays `null`; current macro pressure is never backfilled into the past.

Require at least 15 contiguous pre-event bars reaching the release boundary and
three contiguous post-event bars. Sub-minute releases and incomplete windows
stay unresolved rather than mixing pre/post prices. Resolution requires at least
30 minutes after event end plus recovery or normalized recent ranges. Existing
Phase 3A CPI/NFP/PCE/FOMC blackout windows still apply and cannot be shortened by
absorption. The six-hour analysis horizon and these thresholds are model assumptions.

### Entropy and timeframe tension

Market entropy combines direction-sign Shannon entropy, direction flips and
inverse path efficiency over 25 five-minute closes. It is a choppiness index;
it does not prove a process is random. Score >= 78 vetoes new entries.

Tension compares the signed biases of 1m/5m/15m/1H/4H on equal terms. Complete
directional cancellation is 100, common direction is 0. Convergence is the
decrease from the prior decision's tension. Score >= 85 vetoes entries.
Three state changes across the current and preceding three decisions, within
one hour, trigger the unstable-state veto. Unsupported thresholds are rejected.

## Slow-regime provider interface

Channels: `cot`, `etf`, `physical`, `options`. No real adapter is bundled. The
default is UNAVAILABLE with `null` scores; deterministic examples exist only in
the tests and are labelled `TEST_DATA`. There is no automatic synthetic fallback.

Each trusted adapter declares `name`, `data_mode` and `fetch(now)`. Successful
envelopes include `status=OK`, `data_mode` LIVE/DELAYED/TEST_DATA, and 1..50 records.
The collector sets `provider` and `retrieved_at`. Each record contains:

| Field | Meaning |
|---|---|
| `source`, `url` | Original source and credential-free HTTPS reference |
| `observed_at` | Economic observation time; e.g. COT observation date |
| `published_at` | First public availability, including COT publication lag |
| `score` | Finite -100..100 provider-normalized background index |
| `method` | Named, documented normalization methodology |

Require `observation <= publication <= receipt <= decision time`. Check ages of
both the latest observation and publication per source. Conservative maximum
ages: COT 10 days, ETF 3 days, physical 7 days, options 1 day; stale at equality.
Configured failures, future timestamps, malformed values, stale latest records
and conflicting strong sources veto new entries. Unconfigured channels are
optional background gaps and cannot supply a score.

Synthetic markers at adapter, envelope or record level cannot be promoted to LIVE.
The Phase 3A normalizer recognizes TEST_DATA as synthetic FIXTURE provenance.
Gold acquisition also preserves restrictive payload provenance. Unknown markers
fail closed. TEST_DATA slow context needs explicit injected test permission and
FIXTURE gold; it cannot participate in a real-gold decision. Response guards
also enforce the current permission and recompute slow freshness.

Provider truth remains a trust boundary: code cannot detect an adapter that
deliberately fabricates values while removing every synthetic marker.

## Council and risk behavior

All original technical components, macro/news components, combined scores and
confidence fields remain visible. New fields are `hidden_state`, `slow_regime`,
`analytics_context` and `hidden_integration=RISK_FILTER_NO_SCORE_PROMOTION`.

New hard vetoes include high entropy, unresolved event/market shock, extreme
timeframe tension, unstable state, hidden/council conflict, unavailable/stale
market features and invalid slow freshness/provenance. These add to existing
staleness, event, macro conflict and execution guards. Valid same-provenance 1m
exits continue during entry vetoes. No stop/target is changed by a later state.

## Shadows, persistence and analytics

Three variants run only as isolated hypotheses:

| Variant | Entry evidence |
|---|---|
| TREND_CONTINUATION | Signed DGFE directional pressure, absolute score >= 35 |
| SWEEP_RECLAIM | Signed liquidity pressure, absolute score >= 50 |
| RESILIENCE_DIVERGENCE | Signed resilience residual, absolute score >= 50 |

Each gets an evaluation for every new decision, including NO_TRADE and occupied
variant reasons. Common data/provenance/hidden-risk vetoes apply. Shadow variants
do not use production position occupancy or calibration gates; they are competing
experiments, not copies of the production decision.

At most one active position per variant. A hypothetical entry uses the next full
1m opening price, expires after five minutes, rejects excessive fill gaps, and
has a 1R stop, 2R target and 60-minute horizon. Stop wins same-bar ambiguity;
opening gaps beyond the stop use the worse price. Ordinary missing candles keep
the checkpoint unchanged; scheduled session gaps follow the existing market
calendar policy. Results are gross, without spread, commission or slippage.

Additive Phase 3B tables, created only in the dedicated Phase 3B database:

- `phase3b_context`: prior macro pressure, observation time, source snapshot FK.
- `phase3b_shadow_decisions`: unique decision/variant evaluation and snapshot FK.
- `phase3b_shadows`: separate hypothetical position state with active-variant uniqueness.
- `phase3b_shadow_events`: immutable creation/fill/bar/close event records linked to snapshots.

The existing production `trades`, `trade_events`, episode state and calibration
queries never read shadow outcomes. Shadow work uses a savepoint; errors roll
back its writes and record a generic failure health code without exception text.
Underlying disk/database failure can still affect the shared local service.

Production decisions capture policies, all features, historical expectations and
source modes in hashed snapshots; full decision checksums protect execution
metadata. Replay uses captured inputs and original source code, never current
provider values. Shadow event records reference the input snapshots used for
their hypothetical outcomes. Retain original releases for old-source replay.

Analytics keep production and each shadow variant separate and group by entry
hidden state, session, volatility regime, direction, timeframe alignment and macro
regime. Each group reports sample count, win rate, expectancy/average R, profit
factor and maximum closed-equity drawdown in R. Break-even counts in the sample
but not as a win. Empty estimates and undefined zero-loss profit factor are `null`
with status labels, not infinity or invented performance. This is descriptive
closed-trade analytics, not new calibrated trading evidence.

## Local service and verification

```powershell
$env:PHASE3B_DEMO_DB_PATH = 'backend/data/phase3b.sqlite3'
python -m uvicorn backend.phase3b_main:app --host 127.0.0.1 --port 8003 --workers 1
python -B -m pytest backend/tests -q -p no:cacheprovider
python -m backend.manage --database backend/data/phase3b.sqlite3 replay DECISION_ID
```

Startup rejects Render and configured Phase 1/2/3A database paths. A populated
other-phase database is rejected without rewriting its history. Existing entry
points and the Android/Render URL remain unchanged. No service was deployed.

Read endpoints add `/gold/analytics`, `/gold/shadows` and
`/gold/shadow-decisions`. Shadow lists are paginated; no write or trading endpoint
is added. `/signal` includes component diagnostics; Android remains unchanged.

## Blockers and required real inputs

No known unresolved implementation/test blocker for this offline DEMO phase.
Real-data activation still requires reviewed, licensed provider adapters and
point-in-time validation for:

- XAU/USD: existing `TWELVE_DATA_API_KEY` access (not present in the checked environment).
- Phase 3A: DXY/broad USD history, intraday 2Y/10Y yields, complete revised US
  economic calendar, financial/geopolitical headlines, optional Fed expectations.
- COT: observation and actual release timestamps plus a positioning normalization;
  public reports may need no credential, depending on the chosen adapter.
- ETF flows: timestamped fund-flow/holdings access and entitlement if required.
- Physical market: documented premiums/discounts or other physical context source.
- Options/skew/crowding: licensed observations and a documented normalized index.

No vendor-specific secret names are invented for unselected providers. Secrets
must come from environment/secret management; provider exceptions and credential
URLs must not enter logs. No keys, subscriptions or live feeds were created.

State thresholds, sentiment/pressure mappings, resilience, event windows and
shadow execution assumptions require historical and forward-DEMO validation.
Point-in-time historical archives are required to avoid revision leakage.

**Pre-deployment optimization remains open:** Phase 3A measured about 76 MiB per
1,440 snapshots. An initial Phase 3B fixture measured about 78 MiB snapshot-only,
before accumulated prior context, real provider payloads, decision/trade journals,
shadow events and SQLite overhead. Optimize retention/archival and storage while
preserving replay; do not purge demo history as an implementation shortcut.
Analytics currently aggregate closed trades in memory and need scaling work for
large histories. Add durable storage, backup/restore and local access controls
before operational exposure. See [PHASE3B_REVIEW.md](PHASE3B_REVIEW.md).
