# Phase 3E — local real-data forward DEMO

## Status and boundaries

Implementation is separate from the immutable Phase 3D commit
`bcafa19874bc26b77568a6ad1e08024b4dc325fc`. No earlier backend source, Android
source, backend URL, deployment configuration or broker integration is changed.
Phase 3B is the Champion. Phase 3C remains an isolated DEMO Challenger, with no
promotion endpoint or automatic promotion. Research remains
`INSUFFICIENT_EVIDENCE`; promotion remains `PROMOTION_INELIGIBLE`.

**Actual readiness: NOT_READY.** No real provider credentials, licensed gateways
or verified real captures were supplied. Offline contract tests do not establish
real feed availability, profitability, a 75% win rate or predictive edge.

## Data flow

1. Explicitly approved provider specifications identify an HTTPS endpoint,
   canonical instrument, declared entitlement/mode and environment secret name.
2. Once per UTC minute, durable request identities precede bounded collection.
   HTTP bodies are hashed; only whitelisted normalized data is persisted.
3. Source observation/publication timestamps and actual receipt timestamps are
   distinct. Future or naive timestamps fail closed. Publication may precede
   receipt; scheduled future calendar events are allowed.
4. Append-only editions preserve normalized values, raw hashes, provider/config
   identity, revision, mode and receipt records. Revisions do not overwrite an
   earlier edition. Mode changes cannot reuse a LIVE observation identity.
5. Selection recomputes source age and compares approved sources. Disagreement,
   missing critical feeds, delayed/test/historical inputs and invalid optional
   configured feeds veto new entries. Failover is recorded explicitly.
6. A capture commits before engine evaluation. The Champion, separate Challenger,
   shadows, immutable decision evidence and completed checkpoint commit in the
   engine's single transaction. Recovery uses the original captured inputs/time.
7. Outcomes append when first known, with gross R and separately labelled
   simulated-net R. They never alter earlier decision evidence.
8. Exact Champion and Challenger replays are checked. Readiness re-evaluates
   freshness at the time of the status request.

The existing 1m monitor and 5m/15m/1H/4H logic remain unchanged. A partial XAU feed
can provide independently valid minute exit bars while entry gates remain shut.
Historical gaps before an active trade do not remove otherwise valid exit bars;
the existing position checkpoint enforces continuity where it matters. Conflicting
minute prices never invent an exit. Downtime beyond available minute history
leaves an explicit catch-up gap requiring trusted backfill in a future operation.

## Provider configuration and required real inputs

`ProviderSpec` fields: `name`, `channel`, `endpoint`, `secret_env`, `adapter`,
`mode`, `approval`, `priority`, `timeout_seconds`, `entitlement_delay_seconds`.
Only the first three are structurally required, but a nonempty approval and an
explicit usable data mode are operationally required. Default mode is UNAVAILABLE.
Unknown fields, credential-bearing query strings, URL userinfo, redirects, unknown
adapters and duplicate provider names are rejected. Lower priority wins.

| Channel | Canonical symbol | Adapter / eventual prerequisite |
|---|---|---|
| xau | XAU/USD | Native `twelve_data`; licensed intraday spot-gold entitlement and environment API key, or approved JSON gateway |
| dxy | DXY | Approved JSON USD-index gateway, authorized canonical DXY series and access credential |
| us2y | US2Y | Approved JSON 2Y yield gateway; percentage-point units and suitable intraday updates |
| us10y | US10Y | Approved JSON 10Y yield gateway; percentage-point units and suitable intraday updates |
| calendar | US_CALENDAR | Approved JSON calendar gateway with complete US CPI/NFP/PCE/FOMC coverage, editions and source publication times |
| news | GOLD_USD_NEWS | Approved JSON licensed financial/geopolitical news gateway with source URLs, identifiers and publication times |
| fed | FED_EXPECTATIONS | Optional approved JSON source of meeting/rate expectations |
| cot | GOLD_COT | Optional approved JSON release-aware COT source |
| etf | GOLD_ETF | Optional approved JSON gold ETF flow source |
| physical | GOLD_PHYSICAL | Optional approved JSON physical-market context source |
| options | GOLD_OPTIONS | Optional approved JSON skew/crowding source |

The generic adapter is a concrete HTTP adapter for the schema below. It is **not**
a claim that arbitrary vendor APIs implement that schema. A chosen vendor needs
a licensed, tested schema-compatible gateway before configuration approval.
No DXY proxy, daily yield series or generated headline is silently substituted.
Slow feeds are background indices with documented methods, not probabilities.

Credentials live only in environment variables referenced by `secret_env`.
Approved JSON uses a Bearer authorization header. Twelve Data uses
`Authorization: apikey <environment value>`; it requests UTC explicitly and
checks response metadata. See [official API documentation](https://twelvedata.com/docs)
and [official timezone guidance](https://support.twelvedata.com/en/articles/5745849-timezones).
The native adapter rejects an unverified timezone, rather than assuming that a
naive timestamp is UTC. Confirm actual response metadata and entitlements during
real integration. It derives a closed bar's source timestamp from open time plus
interval; publication is conservatively represented by that close, and actual
availability is bounded by recorded receipt, not inferred from that publication.
Native raw hash is the canonical hash of per-timeframe raw-body SHA-256 hashes.

## Approved JSON response contract

Root fields:

```text
symbol: canonical symbol from the table
data_mode: LIVE_DATA | DELAYED_DATA | HISTORICAL_POINT_IN_TIME |
           TEST_DATA | FIXTURE | UNAVAILABLE
observed_at: timezone-aware source timestamp
published_at: timezone-aware publication timestamp (defaults to observation)
revision_id: explicit stable source edition identifier
value: channel-specific data below
delay_seconds: optional nonnegative provider-reported latency
```

Every nested `data_mode`, `is_synthetic` and `delay_seconds` marker is inspected
before normalization. Configuration may restrict mode; it cannot upgrade a
response. Historical/test/fixture data can be archived but never selected for
real forward decisions. An unavailable or malformed response produces sanitized
failure evidence, never a fabricated observation. A missing provenance marker
does not prove authenticity: provider approval is an operator attestation whose
accuracy still needs independent validation.

- **XAU:** `value` maps `1min`, `5min`, `15min`, `1h`, `4h` to closed candles
  `{t,o,h,l,c,v?}`. `t` is interval open in UTC. Root `observed_at` is latest
  closed 1m bar close. Higher timeframes are validated independently.
- **DXY/yields:** `value` contains `as_of` and `records`. Each record has `id`,
  `source`, `url` (HTTPS, without secrets), `published_at`, `observed_at`,
  `instrument`, `value`. Supply enough timestamped history for momentum.
  Root observation is the latest record observation. Values are positive index
  points for DXY, and percent yields (e.g. 4.25, not .0425) for Treasuries.
- **Calendar:** `as_of`, `records`, `coverage`. Records add `event_key`, `kind`
  (`CPI`, `NFP`, `PCE`, `FOMC`, `OTHER`), `name`, `scheduled_at`, `end_at`,
  `importance` (`HIGH`, `MEDIUM`, `LOW`), `status` (`SCHEDULED`, `CANCELLED`).
  Coverage has `complete:true`, `country:US`, all four critical `event_types`,
  and timezone-aware `start`/`end`. Root observation equals `as_of`.
- **News:** `as_of`, records with `id`, `source`, `url`, `published_at`, `title`,
  `importance`. Root observation equals source `as_of`, not client receipt.
- **Fed:** `as_of`, records with common identity/publication fields plus
  `meeting_at`, `target_lower`, `target_upper`, `expected_rate` (percent).
- **Slow:** records with `source`, `url`, `observed_at`, `published_at`,
  `score` (-100..100) and `method`. Root observation matches latest record.

Source IDs/URLs and original editions remain visible. Existing Phase 3A news
deduplication, recency decay, source conflicts and event blackout rules are reused.
An old anchor may support momentum, but each contributing source's latest usable
observation must meet its freshness limit. A fresh wrapper cannot hide stale slow
records. Root source stamps cannot be replaced by receipt time.

## Health and readiness

Health: HEALTHY, DEGRADED, STALE, UNAVAILABLE, CONFLICTING. Source age limits are
150s XAU, 300s DXY, 900s yields/news, 3600s calendar, 1 day Fed/options,
10 days COT, 3 days ETF, 7 days physical. These conservative defaults require
provider/session validation. Market closures may intentionally make readiness
NOT_READY. Existing engine freshness/event vetoes also remain in force.

Cross-source disagreement thresholds are conservative **engineering assumptions**:
90s source timestamp difference; XAU max($2, 0.1%); DXY 0.15 index points;
yields 0.05 percentage points; unequal matching-event scheduled times. They are
not optimized strategy thresholds. Sources may legitimately differ by venue,
quote convention or publication cadence; investigate rather than weakening gates
to obtain trades.

- NOT_READY: required provider/provenance/clock/freshness/monitor/integrity checks
  fail, or companion evidence is incomplete.
- DATA_READY: valid current sources, pending replay bootstrap.
- FORWARD_DEMO_READY: valid real modes and clocks, fresh critical feeds, healthy
  storage, both replays verified, and no unresolved integrity blocker.

Initial new-entry veto lasts until Champion replay succeeds. Challenger failure
blocks full evidence readiness but never changes Champion scoring or routing;
its replay/health controls are separate. No readiness value authorizes a broker
order or establishes profitability. A healthy forward runner can correctly decide
NO_TRADE due to an event window or strategy/risk veto.

## Local operation (not deployment)

Keep provider configuration outside committed files if it contains private
endpoint details. It must contain only secret **references**, never secret values.
Use a new database path; earlier phase databases are refused. Changing provider
configuration or source identity requires a separate evidence database.

```powershell
python -B -m backend.forward --database <new-local-forward.sqlite> --config <approved-providers.json> --once
python -B -m backend.forward --database <same-local-forward.sqlite> --config <approved-providers.json> --status
python -B -m backend.forward --database <same-local-forward.sqlite> --config <approved-providers.json>
```

An empty configuration array is safe: it records unavailable-provider NO_TRADE
evidence and stays NOT_READY. No synthetic provider is imported by the service.
The service refuses the Render environment. No service is started by importing
the package. Ctrl+C stops the local scheduler. Only one writer process may own a
database; the OS lock releases on process exit.

Provider calls use bounded daemon workers, finite HTTP timeouts, response-size
limits, no redirects and no environment proxy inheritance. Each provider/frame
has at most one in-flight call. Failures use exponential backoff, bounded
Retry-After handling, and a five-minute circuit after three failures. Late results
cannot be reused as earlier observations. Duplicate requests are not retried in
the same minute; durable successful receipts recover after a crash. Interrupted
requests remain explicit gaps, never assumed successful.

Storage and transaction errors stop evaluation. Capture replay resumes before
fresh collection. Missed schedule intervals are explicit ledger events; the
service does not pretend a new download was observed during downtime.

## Evidence/storage/security

`forward_objects` is a compressed content-addressed store. `forward_observations`
records editions, `forward_receipts` records acquisitions, `forward_ledger` is a
hash chain, and `forward_cycles` is the crash-safe work journal. Native engine
snapshots are transparently compressed in this **dedicated** database; existing
replay code receives exactly the original canonical JSON.
Capture observation lists, selected providers and provider-health views share
the same immutable observation documents. Those documents reference shared
normalized values; acquisition-specific receipt time and health are preserved
separately. Companion decision inputs/results are also transparently compressed.

UPDATE/DELETE triggers protect archived objects, observations, receipts,
snapshots, decisions and event journals. Full startup/status checks verify object
hashes, snapshot hashes, ledger chain and checkpoint references. Incremental tick
checks verify newly appended immutable data to avoid quadratic full-history scans.
Hashes are tamper evidence; they are not an external signature against an
administrator who can rewrite the database and remove triggers.

Raw responses are never logged or stored. Normalized text is checked against
configured secret values before persistence. Errors use fixed codes. Tests use
temporary random sentinels, never usable credentials. Protect the environment,
database directory and backups with host access controls; this phase adds no
remote administrative endpoint.

Demo cost assumptions remain the unchanged Phase 3C `DemoCosts` policy. Gross
fills/outcomes and simulated net R are separate; these costs are not observed
broker spreads or executable quotes: spread is 0.30 price points, slippage is
0.10 per side, and extra latency penalty is zero. Net R subtracts 0.50 divided by
initial risk in price points from gross R. See [the review/storage report](PHASE3E_REVIEW.md) for measured
compression, limitations, and the still-open pre-deployment capacity item.

## Remaining operational prerequisites

Approve licensed providers/gateways and entitlements; provision environment
credentials; verify live source identity, UTC conventions, cadence, units,
calendar coverage, revisions, failover and independent cross-source agreement.
Run sustained local captures, restore/replay drills and storage profiling with
real news and mature histories. Missing real evidence cannot be filled with
synthetic results. No current deployment, historical edge claim, model promotion,
Android change, broker trading or Phase 3F is included.
