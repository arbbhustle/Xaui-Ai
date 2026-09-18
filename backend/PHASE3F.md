# Phase 3F — native provider infrastructure (DEMO only)

## Current state

**NOT_READY. Real collection is disabled. All providers below are unapproved candidates. No production credentials or subscription entitlements have been supplied or validated.**

The shipped `realdata/disabled-config.json` has no providers, `collection_enabled: false`, and a disabled, usage-unvalidated Forex Factory cross-check. No deployment, broker integration, Android change, or automatic strategy promotion is included. Phase 3E and all earlier tracked source files remain unchanged. The Champion remains Phase 3B; the Challenger and three shadow variants remain isolated DEMO research.

## Candidate adapters and outstanding approvals

| Candidate | Channels | Suggested environment reference | Required validation |
|---|---|---|---|
| Twelve Data | XAU/USD 1m, 5m, 15m, 1H, 4H | `TWELVE_DATA_API_KEY` | Licensed spot Gold/USD instrument, USD per troy ounce, UTC response semantics, intraday/real-time entitlement, quotas and authentication |
| Trading Economics | DXY, US 2Y, US 10Y, US calendar | `TRADING_ECONOMICS_API_KEY` | DXY:CUR, USGG2YR:IND, USGG10YR:IND mappings and access; index points versus percent yields; timezone/cadence; complete calendar coverage and release/revision semantics |
| Finnhub | Financial/Gold/USD/Fed/geopolitical news | `FINNHUB_API_KEY` | News license, coverage, latency, authentication, publication/revision behavior and quotas |
| Forex Factory | Optional secondary USD event cross-check | No key assumed for the public export | Usage permission, permitted polling/caching, JSON schema, Last-Modified semantics and coverage; disabled until validated |

These environment names are references, not keys. Providers can be configured separately by channel and priority. Approval, entitlement reference and expiry, live-data permission, instrument units and UTC semantics are explicit configuration fields. Configured credentials alone do not establish approval. Changes to configuration or implementation require a new dedicated Phase 3F database; existing evidence remains reproducible with its original code/configuration.

No optional Fed expectations, COT, ETF, physical-market or options provider was invented or enabled. The unchanged intelligence layer continues to distinguish missing inputs.

## Data flow and authenticity

1. The disabled-by-default local runner checks its collection flag before acquisition.
2. Approved native adapters use fixed HTTPS hosts, header-only credentials, bounded response sizes, timeouts and no redirects. Phase 3E's durable request journal provides primary-provider rate-limit backoff, retries, circuit breaking and recovery.
3. Native schemas are normalized before persistence. Checks cover exact instrument, units, source/receipt chronology, closed candles, malformed/nonfinite numbers, source frequency and synthetic markers. An injected HTTP transport always produces TEST_DATA.
4. The native verification journal checks actual authentication evidence, operator-approved entitlement, provenance, freshness and update cadence. Three qualifying samples spanning at least two minutes are required; a successful HTTP response alone is insufficient. Daily/delayed data, missing approval, test markers and incomplete critical coverage cannot establish a healthy LIVE feed.
5. Shared content-addressed provider objects, acquisition receipts and capture references preserve each edition. A snapshot contains the exact inputs available at decision time. Native source hashes normalize line endings through UTF-8 text reading.
6. The existing council evaluates the capture. Provider and optional secondary-calendar vetoes are risk-only additions. A verified fresh 1m feed can still monitor an open position when higher frames are missing or malformed; entry remains vetoed. Historical minute gaps are checked at the position checkpoint by the unchanged engine.
7. The same transaction records decisions/checkpoints. Outcome analytics use a separate savepoint so failure cannot roll back the Champion. An analytics failure creates a persistent readiness blocker.

XAU checks include market closure, timestamp repetition, repeated OHLC, abnormal jumps, duplicate candles, and bid/ask order and freshness when provided. Candle-only responses are explicitly identified as lacking executable quotes. The existing selection layer records discrepancies and switches between independently configured sources; no second XAU vendor is approved or claimed here.

DXY/yield momentum uses only verified prior receipts available before the snapshot. The one-hour anchor must exist; missing history vetoes signals and prevents FORWARD_DEMO_READY. No downloaded future edition or fixture is used to fill the history. Independent yield publication timestamps are preserved when combining envelopes.

## Calendar, news and Forex Factory

Calendar editions retain event identity, scheduled UTC time, receipt time, source update, forecast, previous, actual, revised value and reference where provided. `first_observed_actual` is preserved across later editions. **It is not claimed to be the original first release if collection began late or the provider cannot certify that fact.** Release quality is explicitly `AS_RECEIVED_NOT_VERIFIED_ORIGINAL_RELEASE`. An authoritative original-release archive remains a provider-validation requirement.

News retains provider/publisher identity, article ID, headline, URL, publication/receipt times, content revision, relevance and normalized syndication identity. The unchanged news engine deduplicates IDs, URLs and syndicated headlines, retains earliest publication for decay, and detects contradictory editions. Irrelevant new headlines cannot refresh old relevant news. Later receipts cannot rewrite an earlier snapshot.

Forex Factory uses the official weekly JSON export linked from its calendar. No HTML scraper or login automation is used. Its default polling interval is five minutes, subject to approval of the actual usage policy. The export must supply a usable Last-Modified timestamp; generic HTTP Date does not establish source freshness. Ambiguous/all-day/naive event times fail closed. Explicit offsets are converted to UTC.

The cross-check keeps both providers' events and values. It conservatively matches CPI, NFP, PCE, rate decisions and Fed speeches, distinguishing core/headline, monthly/yearly and named speakers. ADP is not NFP. Exact duplicate exports are collapsed; ambiguous/unmatched events remain visible. High-impact conflicts from an available, non-test secondary export add a veto. Stale/unavailable exports remain recorded and cannot establish current agreement. TEST exports cannot affect production decisions. Forex Factory is never selected as the primary calendar and can never establish LIVE_DATA or readiness by itself. Missing export fields remain null.

## Outcomes and reports

Finalized outcomes include gross R, simulated net R, spread/slippage assumptions, duration, MFE/MAE bounds and final outcome timestamp. The unchanged simulation assumes **0.30 price points spread plus 0.10 points slippage per side**, zero added latency; net R subtracts 0.50/risk points. These are assumptions, not measured execution costs.

Minute candles cannot reveal whether the extreme occurred before or after an intrabar exit. Consequently MFE/MAE have explicit lower/upper bounds; incomplete causal bars yield unavailable values. Only candles already closed in an as-received snapshot are eligible. Outcomes are immutable and exactly-once; corrections require separate explicit audit records, never replacement. No correction or historical rewrite was performed.

Read-only reports show real-provider-qualified cycle and 5m decision counts, BUY/SELL/NO_TRADE counts, closed outcomes, net/gross R, expectancy, profit factor, drawdown, calibration, regime/session/strategy cohorts and provider health history. A later validated tick cannot qualify an earlier fixture decision. Companion and shadow results never enter the Champion cohort or change its routing.

Research thresholds require at least 100 independent closed trades, 20 dated sessions, three hidden regimes and 30 days spanned by observed trades. Idle wall time cannot satisfy the elapsed-period threshold. These are minimum research filters, not validated guarantees of sufficient statistical power. Until qualified, reports say **INSUFFICIENT_FORWARD_DATA**. No profitability, predictive-edge or 75% win-rate claim is established.

## Readiness and operations

- **NOT_READY:** missing/unapproved/unverified providers, synthetic contamination, stale inputs, conflicts, collection disabled or integrity blockers.
- **DATA_READY:** all critical sources have genuine validated provenance and current health; operational qualification is still incomplete.
- **FORWARD_DEMO_READY:** additionally requires 16 consecutive minute captures, restart recovery, both replays, sufficient momentum history, healthy storage/ledger and no critical blocker. This says nothing about profitability.

For a disabled local status/report database:

```powershell
python -B -m backend.realdata --config backend/realdata/disabled-config.json --database <new-local-phase3f-database> --action status
```

Actions are `status`, `once`, `run`, `report`, `capacity`. `run` rejects a disabled configuration; `once` does not acquire data while disabled. Render execution is explicitly rejected. Do not reuse a Phase 3E database. No operational collection was enabled for this work.

Before any real Forward DEMO start: obtain approval and credentials, validate the instrument/entitlement/timezone and calendar/news contracts, test actual provider latency and failure behavior, provision storage/backups, then collect genuine evidence in a new DEMO database and verify restart/replay gates. Quiet calendar/news feeds may remain stale under the conservative source-update policy; a validated freshness/coverage contract is required rather than using receipt time as a substitute.

## Verification and references

Offline tests use dynamically generated sentinel credentials and mock transports. They are not real provider certification. See `PHASE3F_REVIEW.md` and `PHASE3F_STORAGE.md` for final results and limitations.

Primary adapter references: [Twelve Data documentation](https://twelvedata.com/docs), [Trading Economics authentication](https://docs.tradingeconomics.com/get_started/), [market symbols](https://docs.tradingeconomics.com/markets/symbols/), [calendar fields](https://docs.tradingeconomics.com/economic_calendar/schema/), [Finnhub news](https://finnhub.io/docs/api/market-news), [Forex Factory calendar/export links](https://www.forexfactory.com/calendar), [official JSON export](https://nfs.faireconomy.media/ff_calendar_thisweek.json). Export availability is not approval to enable collection.
