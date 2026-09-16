# Phase 3A — source-stamped macro/news intelligence

Base: Phase 2 commit `43d4b1d263c382a4dcbafe44a12caad8b7aad51a`.
This is a separate local, DEMO-only service and database. There are no Android
changes, broker orders, Render deployments, push notifications or Phase 3B work.
The production Render URL and existing entry points are unchanged.

## What is implemented versus unavailable

Implemented: provider protocols, independent validation, bounded acquisition,
point-in-time macro/news scoring, event blackouts, source conflict vetoes,
news deduplication/decay, council integration, persistent snapshots and replay.

No macro/news provider adapter or real credentials are installed. The checked
environment also has no `TWELVE_DATA_API_KEY`. Default intelligence providers
return UNAVAILABLE with empty records. They never substitute synthetic values,
cached failures, a fabricated calendar, or data labelled LIVE. The default Phase
3A service therefore vetoes new entries until critical sources are connected.

Deterministic synthetic providers exist only in `tests/intelligence_fixtures.py`.
Their output is marked FIXTURE, and entry using fixture intelligence is rejected
unless `IntelligencePolicy(allow_fixture_data=True)` is explicitly injected.
There is no service environment switch that silently enables fixture data.
Fixture, delayed and live outcomes have separate calibration model identities.

## Local entry point

```powershell
$env:PHASE3A_DEMO_DB_PATH = 'backend/data/phase3a.sqlite3'
backend/.venv/Scripts/python -m uvicorn backend.phase3a_main:app --host 127.0.0.1 --port 8002 --workers 1
```

The factory refuses the configured Phase 1/2 database paths. The engine refuses
a database containing other-phase decisions. Startup with `RENDER` set is refused
pending a separate deployment review. No running service is started by this patch.

## Provider contract

`IntelligenceProvider` exposes a stable `name`, registered `data_mode`, and `fetch(now) -> dict`.
Successful unregistered modes fail closed. Adapter or record FIXTURE markers
cannot be promoted to LIVE by an envelope; DELAYED cannot be promoted either.
The XAU feed separately records its provider, mode and retrieval timestamp.
Missing XAU provenance blocks entries. Fixture and real-input runs require
separate databases; incompatible XAU provenance freezes position checkpoints
instead of applying unrelated prices to existing trades. API responses enforce
the current fixture permission as well as the decision's captured permission.
An adapter is trusted application code supplied to `create_phase3a_app(providers=...)`;
there is no arbitrary-URL fetch endpoint, public data injection route or dynamic
plugin loader. Adapters must enforce their own HTTP timeouts, use environment or
secret-manager credentials, and respect provider licensing and redistribution rules.

Five independent channels are supported: `usd`, `yields`, `macro`, `calendar`,
`news`. An adapter may aggregate multiple traceable publishers within a channel.

Each successful envelope contains:

| Field | Meaning |
|---|---|
| `status` | `OK`; otherwise `UNAVAILABLE` |
| `data_mode` | `LIVE`, `DELAYED`, or `FIXTURE`, according to source provenance |
| `as_of` | Provider's coverage/update timestamp with timezone; **not fetch time** |
| `records` | At most 1,000 normalized records |
| `coverage` | Required calendar coverage attestation, below |

The collector stamps `retrieved_at` from its own clock and stores the adapter's
name. All successful records require `id`, original publisher `source`, an HTTPS
traceability `url`, and timezone-aware `published_at`. Unknown fields are dropped;
article bodies, arbitrary provider responses and exception messages are not stored.
Credential-bearing URLs are rejected, and tracking query parameters are removed.
Dates normalize to UTC. Non-finite numbers and malformed channels are rejected
independently before snapshot hashing.

Additional record fields:

| Channel | Fields and units |
|---|---|
| USD | `instrument`: DXY or explicitly labelled USD_BROAD; `observed_at`; `value` in index points |
| Yields | `instrument`: US2Y or US10Y; `observed_at`; `value` in annual percent, e.g. **4.25**, not 0.0425 or 425 |
| Macro | `meeting_at`; `target_lower`, `target_upper`, `expected_rate`, all in annual percent |
| Calendar | `event_key`, `kind` CPI/NFP/PCE/FOMC/OTHER, `name`, `scheduled_at`, `end_at`, `importance` HIGH/MEDIUM/LOW, `status` SCHEDULED/CANCELLED |
| News | `title`, `importance` HIGH/MEDIUM/LOW |

`event_key` must identify the same economic occurrence across sources, such as
`US:CPI:2026-09`. A calendar envelope must attest `coverage.complete=true`,
`country=US`, all four critical `event_types`, plus `start` and `end`. Coverage
must include the preceding six hours and following 24 hours. Empty records are
safe only with this positive coverage attestation. No monthly release dates are
guessed. Actual/forecast economic release values are not consumed in Phase 3A.

The collector starts channels independently with a shared two-second acquisition
budget. A stalled channel is unavailable for that tick; at most one in-flight
request per channel is retained, so retries cannot accumulate blocked threads.
Late responses keep their original source and retrieval timestamps. Network
adapters still need bounded I/O; the collector cannot cancel an arbitrary library
call. Provider errors never prevent valid 1m position monitoring from proceeding.

## Point-in-time and freshness rules

All intelligence is evaluated at the recorded decision observation time, just
like 1m timing. Technical 5m/higher-timeframe cutoffs remain unchanged. Require:

`observation <= publication <= source as_of <= retrieval <= decision time`

Scheduled future calendar events and future Fed meetings are legitimate future
dates; future publication/observation/receipt timestamps are not. Later schedule
revisions and later news cannot be used in an earlier snapshot. Snapshot history
is immutable; current feeds and revised data are never fetched during replay.

| Channel | Maximum source age (stale at equality) |
|---|---:|
| USD | 300 seconds |
| US yields | 900 seconds |
| Fed expectations | 86,400 seconds |
| Calendar | 21,600 seconds |
| News feed coverage | 900 seconds |

USD/yield observation timestamps are checked independently of envelope age;
fresh polling cannot disguise yesterday's quote. Fed expectation publication
age is also checked. Source audits expose envelope age, observation details,
freshness and validation issues. News headlines decay independently from feed
coverage freshness: an up-to-date feed may honestly report no new relevant story.

`/signal`, `/health`, `/market-status`, `/performance` and `/trades` recalculate
intelligence health when served. A blackout beginning between worker ticks blocks
the next response. `/signal` checks both the latest worker snapshot and its own
decision snapshot, so refreshed worker data cannot relabel an old decision fresh.
These are read-only response guards; stored decisions remain unchanged. `/history`
continues to mark archived decisions historical/stale rather than live.

## Separate scores and assumptions

Directional intelligence scores range from **−100 (bearish gold) to +100 (bullish
gold)**. Missing data is `null`, never a neutral fabricated observation.

| Score | Calculation |
|---|---|
| USD | Negative of clipped one-hour index percent change divided by 0.3% |
| Yields | Negative of clipped one-hour yield change divided by 5 basis points; average 2Y and 10Y equally |
| Macro | Negative of expected Fed rate minus current target midpoint, divided by 25 basis points; nearest supplied future meeting |
| News sentiment | Explicit headline lexicon, relevance, provider importance and recency decay |
| Event risk | 0–100, non-directional; imminent/active critical blackout is 100 |

For momentum, the anchor is at least one hour before the latest observation and
no more than 15 minutes older than that target. Direction, change, units, anchor,
current value and source timestamps are visible. Multiple providers are averaged
within an instrument before instruments are combined, so one tenor cannot gain
weight merely through duplicate provider coverage. A broad USD proxy is named
USD_BROAD and is never relabelled as DXY.

These inverse USD/yield and rate-expectation mappings are explicit initial model
assumptions, not guarantees about gold price behavior. They need historical and
forward-demo validation before operational reliance.

News uses `RULE_LEXICON_V1`: explicit phrases such as dollar weakness, falling
yields, dovish/cut language and escalating conflict versus the opposite phrases.
Ambiguous mixed-polarity or negated/rumor headlines receive zero sentiment. This
is a deterministic heuristic, not an LLM assessment or validated sentiment model.
Relevance is based on gold/USD/macro/geopolitical terms. Importance weights are
1.0/0.5/0.2. News impact halves each hour and expires after six hours.

Publisher ID, canonical URL and normalized title identify duplicate/syndicated
stories. All source references remain visible, but a story contributes only once.
Its earliest publication controls decay; republication cannot rejuvenate it.
An additive `phase3a_news_identity(identity PRIMARY KEY, story, first_at)` table
retains hashed identities and earliest publication across polls and restarts.
It is indexed by story and updated inside the tick transaction. Earliest times
are captured in snapshots, so replay never reads today's identity journal.
Deduplication cannot hide conflicting high-impact reports about that story.

## Council integration and hard vetoes

The original five technical components remain in `components`, all timeframe
analysis stays in `timeframes`, and original aggregate scores are in
`technical_scores`. New scores live in `intelligence_components` and the complete
audit in `intelligence`. `combined_scores` exposes the new aggregate.

Blend: **70% technical, 10% USD, 8% yields, 5% macro, 7% news**. A signed gold
score converts to BUY support with `50 + score/2`, SELL with `50 − score/2`.
Optional missing rate expectations are excluded and remaining weights normalized;
the omission is explained. Missing critical scores yield no combined aggregate
and a hard veto. Event risk is a hard guard, not directional evidence.

Phase 2 confidence calibration is applied to the **combined** score, using only
this Phase 3A model's comparable completed outcomes. Its historical-label cutoff,
30-sample threshold, stricter 80-score demo warm-up and 0.55 confidence gate remain.
The captured provider policy and provenance mode participate in model identity.
No Phase 2 or fixture outcomes are silently reused to calibrate live inputs.

In addition to the existing technical and execution gates, hard vetoes cover:

- Missing required USD, both yield tenors, news coverage or critical calendar data.
- Stale or future-dated intelligence, malformed required channels, and a failed
  configured macro provider. Unconfigured optional Fed expectations are distinct
  from a failed or stale supplied feed.
- Opposing strong gold impacts (at least ±0.6) from different high-impact sources;
  conflicting event schedules/statuses or equal-timestamp incompatible revisions.
- Fixture inputs unless explicitly opted in for offline/demo testing.
- Critical event windows, inclusive of both boundaries:

| Event | Before start | After end |
|---|---:|---:|
| CPI | 60 minutes | 30 minutes |
| NFP | 45 minutes | 30 minutes |
| PCE | 30 minutes | 20 minutes |
| FOMC | 90 minutes | 60 minutes |
| Other HIGH importance | 30 minutes | 15 minutes |

An event's explicit duration extends the post-event window (up to four hours).
CPI/NFP/PCE/FOMC cannot evade the guard through a lower importance label. Decisions
expose seconds until the event and PRE_EVENT/DURING_EVENT/POST_EVENT handling.
Open trades keep receiving valid exit monitoring during all veto conditions.

## Persistence, replay and tests

Every decision stores source names/URLs, source and retrieval timestamps,
freshness, all component scores, reasons and vetoes. Normalized provider records,
policies and calibration outcomes are hashed in its snapshot. The additive news
identity table does not rewrite history. Trade metadata retains its entry intelligence.
Decisions additionally checksum execution context and monitor veto metadata.
Checksums detect accidental corruption, not an attacker able to rewrite all hashes.
Portable source hashes include all intelligence/scoring/simulator modules.

```powershell
backend/.venv/Scripts/python -m pytest backend/tests -q
backend/.venv/Scripts/python -m backend.manage --database backend/data/phase3a.sqlite3 replay DECISION_ID
```

Replay dispatch supports Phase 3A and compares the complete intelligence audit,
technical and combined scores, calibration and vetoes. Keep the original release
for older-source replay; committed Phase 2 histories still use its original commit.
All tests are offline with explicit synthetic fixtures and isolated databases.

## Remaining integration requirements

Before real-provider demo use, choose and implement licensed adapters with:

1. DXY or a clearly identified broad USD index: fresh timestamped history and its
   API credential/entitlement, if required by the selected provider.
2. US 2Y and 10Y yields: sufficiently frequent observations plus source publication
   times and provider access. Daily series alone fail the present intraday policy.
3. US economic calendar: complete CPI/NFP/PCE/FOMC coverage, revisions, importance,
   timezone-aware schedules and any required calendar API credential.
4. Financial/geopolitical headlines: original publisher, URL, publication time,
   importance, coverage update time and any required news API credential/license.
5. Optional Fed rate expectations: documented methodology, target range, meeting
   date, expectation timestamps and any required entitlement.
6. Existing XAU/USD candle access via `TWELVE_DATA_API_KEY`.

No vendor-specific key names are invented or hardcoded for the unselected macro
providers. Supply secrets only to reviewed adapters through environment/secret
management. No API subscription has been purchased or configured.

Open limitations: heuristic sentiment and impact mappings are unvalidated;
provider truth/coverage cannot be independently proven by this contract alone;
revised historical feeds require original point-in-time archives for backtests;
snapshot volume, durable-storage/backups and public endpoint access controls still
need an operational plan. These limit real-data activation, not offline testing.
Stop before commit, deployment, Android changes or Phase 3B.

See [the final forensic review](PHASE3A_REVIEW.md) for fixes, regression evidence,
performance measurements and outstanding operational blockers.
