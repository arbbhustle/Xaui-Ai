# Phase 2 — technical council, demo only

Built on Phase 1 commit `5705277e1db0301d9f5fa96756cbf890379cb85f`.
Phase 2 is a parallel local service. It shares the corrected transaction, minute
monitor, fill/exit simulator, stale-response guard, and persistence mechanisms.
It does not change the Android project or its Render URL and adds no broker path.
DXY, USD context, news, Fed, economic calendar and push notifications are deferred.

## Run locally

From the repository root, using the existing backend environment:

```powershell
$env:PHASE2_DEMO_DB_PATH = 'backend/data/phase2.sqlite3'
backend/.venv/Scripts/python -m uvicorn backend.phase2_main:app --host 127.0.0.1 --port 8001 --workers 1
```

The existing `TWELVE_DATA_API_KEY` environment variable supplies real provider
credentials; no key is stored in source. Missing credentials fail closed.
The default Phase 1 database is `backend/data/demo.sqlite3`. The Phase 2 entry
point refuses that configured path, a database containing Phase 1 decisions,
and startup in a Render environment. Nothing has been started or deployed by
this implementation. Run only one worker per database.

## Data flow and timing

1. The existing worker fetches 1m, 5m, 15m, 1H and 4H candles independently.
2. Each feed is validated before hashing. Forming bars are excluded.
3. Inside the SQLite transaction, the engine captures eligible historical demo
   outcomes and strategy settings with the source candles in a hashed snapshot.
4. The pure council computes components, a candidate, confidence and risk vetoes.
5. Existing positions reconcile unprocessed 1m candles chronologically, even
   during higher-timeframe failure. Later vetoes cannot erase earlier fills/exits.
6. At most one new decision per 5m close is journalled. Position, daily-loss,
   duplicate-episode and expiry gates run before a pending demo trade is created.
7. API reads return component explanations and recalculate source freshness;
   reads cannot create trades or change historical records.

| Timeframe | Role | Information cutoff |
|---|---|---|
| 1m | Timing and existing-position monitoring | Latest completed minute at observation |
| 5m | Entry components and plan | Originating completed 5m close |
| 15m | Confirmation | Completed bars at or before the 5m close |
| 1H | Directional bias | Completed bars at or before the 5m close |
| 4H | Macro trend bias | Completed bars at or before the 5m close |

“Macro trend” here means longer price trend. There is no macroeconomic feed.
Sixty completed bars are required for entry analysis; up to 120 feed each feature.
Open-position monitoring retains its checkpoint-only validation and does not
require a full entry lookback. Missing required exit bars remain unresolved.

## Component and meta-decision model

Each directional component returns BUY and SELL support on a 0–100 scale.
These supports are measurements, not win probabilities.

| Component | Weight | Calculation |
|---|---:|---|
| Technical | 30% | ATR-normalized EMA 8/21 distance, price/EMA 55 distance, EMA 8 slope |
| Momentum | 20% | RSI displacement from 50 and recent candle pressure |
| Market structure | 20% | Higher/lower highs and lows plus range breakout position |
| Volatility | 10% | ATR stability quality, shared by both directions |
| Multi-timeframe alignment | 20% | 1m timing 10%, 15m 35%, 1H 30%, 4H 25% |

The signed technical score combines clipped trend distance (45%), longer trend
distance (30%) and slope (25%). Momentum combines RSI and pressure equally.
Structure combines higher/lower swings (65%) and breakout position (35%).
Signed evidence maps to BUY support as `50 × (1 + evidence)` and SELL as its
complement. Volatility quality is `100 − 50 × |log2(ATR / previous ATR mean)|`,
clamped to 0–100, and becomes zero for extreme volatility or zero ATR.

Timeframe bias combines technical evidence 50%, momentum 25% and structure 25%.
Evidence at least +0.15 is BUY, at most −0.15 SELL, otherwise NEUTRAL. Flat price
series use neutral RSI for this council rather than the legacy helper's 100.

The weighted score must reach 68 with a BUY/SELL edge of at least 14. Otherwise
the candidate is NO_TRADE. All 15m/1H/4H biases must agree with a candidate;
opposite 1m timing also vetoes it. Neutral higher-timeframe bias blocks entry.

The existing ATR-based stop-distance floor and 1.25R/2.10R targets are retained.
Regime labels are TREND, RANGE, TRANSITION or HIGH_VOLATILITY. Demo execution
still uses the next eligible full-minute open, stop-first candle ambiguity,
and gross results without spread, commissions or financing.

## Confidence and approved demo warm-up

Confidence targets a **positive-R completed gross demo trade**, not an arbitrary
market-direction prediction or broker execution outcome. It uses direction-
specific score bins `[0,20)`, `[20,40)`, `[40,60)`, `[60,80)`, `[80,100]`.
For the matching bin, the smoothed estimate is `(wins + 2) / (samples + 4)`.
At least 30 comparable outcomes are needed before exposing that estimate.

The training universe is the last 500 eligible, journalled, CLOSED demo outcomes
in the candidate's direction and score bin, from the exact same model identity.
Other bins cannot evict that evidence and accidentally restart warm-up.
Both closure time and the time the closure
became known must be no later than the originating 5m cutoff. Cancelled, open,
Phase 1, different-model and unjournalled results are excluded. The model identity
includes source identity, risk policy and council settings, so changes start a
new calibration population instead of silently mixing incompatible experiments.

- During warm-up, score must reach **80**, with every other risk gate still active.
- `confidence_kind=UNCALIBRATED_WARMUP`, `calibrated_confidence=null`, and numeric
  `confidence=0` explicitly mean no calibrated estimate is available.
- An accepted warm-up demo is labelled `warmup_trade=true`; vetoed decisions are false.
- Once the bin has 30 outcomes, `confidence_kind=EMPIRICAL_DEMO_BETA_BIN` exposes
  the smoothed estimate. Confidence below **0.55** blocks entry. Low confidence
  cannot fall back to warm-up.
- Disabling `CouncilPolicy.allow_demo_warmup` blocks entry until calibration is ready.

The API includes sample count, wins, bin, prior, target and training digest.
This implements an empirical calibration method; no real dataset or out-of-sample
accuracy has been established by synthetic tests. Selection bias, limited sample
size and regime changes remain limitations. Do not describe this as a validated
probability of profit in live trading.

## Risk vetoes

Phase 1 data validity/freshness, session, entry drift, fill gap, kill switch,
daily losing-R cap, active-position, episode deduplication and expiry gates remain.
Phase 2 adds explicit timeframe/timing disagreement, insufficient component score,
insufficient calibration/confidence, and extreme volatility gates.

Extreme volatility on **any** timeframe vetoes entry when ATR is at least 1.8×
the preceding 40 ATR mean, the latest true range is at least 3× the previous ATR,
or ATR is at least 1.5% of price. All thresholds are versioned and snapshotted.

## Persistence and replay

No schema migration or destructive history rewrite is required. Existing JSON
payloads gain components, model identity, confidence metadata and captured
calibration samples. Trade metadata retains the entry decision's confidence.
The full council output is checked by replay, including reasons and component
breakdowns, in addition to Phase 1's plan and veto checks. Later outcomes cannot
alter earlier replay because the calibration dataset is inside the snapshot.

```powershell
backend/.venv/Scripts/python -m backend.manage --database backend/data/phase2.sqlite3 replay DECISION_ID
backend/.venv/Scripts/python -m backend.manage --database backend/data/phase2.sqlite3 backup phase2-backup.sqlite3
```

Source identity includes all council, calibration and simulator modules, with
normalized line endings. Semantic source changes require the original release
for replay. To replay decisions produced by the committed Phase 1 release, use
commit `5705277e1db0301d9f5fa96756cbf890379cb85f`; do not rewrite its hashes.

## Validation and remaining limits

```powershell
backend/.venv/Scripts/python -m pytest backend/tests -q
```

Tests run offline with synthetic candles, explicit synthetic historical outcomes,
mocked HTTP and temporary databases. They cover both phases, timeframe opposition
and neutral bias, trend reversal, every stale/missing feed, volatility, warm-up,
confidence vetoes, future-outcome/late-knowledge exclusion, actual closed-trade
calibration ingestion, concurrent deduplication, recovery, backups and replay.

Before any separately authorized activation: review feed cadence and quotas,
collect real demo evidence, validate calibration out of sample, and plan durable
storage and backups. Snapshot retention and database growth remain Phase 1
operational limitations. Public endpoints still have no authentication or rate
limiting; the local command binds only to loopback. Nothing here authorizes a
deployment, Phase 3 integration or live broker trading.
