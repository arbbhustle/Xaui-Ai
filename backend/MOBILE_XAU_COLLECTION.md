# Mobile-v2: opt-in Twelve Data XAU/USD collection

Implementation only: no deployment, provider request with a real credential, approval,
or Render environment mutation has been performed. Android and engine logic are unchanged.

## Operating contract

Entrypoint remains `backend.mobile.api:app`, one Uvicorn worker/Render instance and a
dedicated persistent SQLite file. HTTP endpoints remain read-only. A lifespan-owned
scheduler polls at most once per 60 seconds after the preceding cycle completes;
slow cycles produce explicit gaps, never a catch-up burst or invented candles.
The database owner lock is retained until the scheduler exits on shutdown.
Neither `RealRunner` nor the local-only forward runner constructor is called.
The scheduler reuses existing cycle, checkpoint, immutable evidence and replay algorithms.

Only Twelve Data XAU/USD is queried: five `/time_series` requests per poll for
1min, 5min, 15min, 1h and 4h, with `timezone=UTC`. Header-only authentication and
intraday timezone selection follow the [official Twelve Data API documentation](https://twelvedata.com/docs).
This can require roughly 7,200 endpoint requests per continuously running day before
rate-limit backoff; actual account credit costs/entitlements must be verified by the operator.

All five frames must be present, valid, closed and fresh before an observation can
be archived. Forming candles are excluded; future candles, invalid symbols, non-UTC
offsets, synthetic markers, delayed data, oversized candle sets and reflected secrets
are rejected. RealStore additionally requires advancing source timestamps across at
least three observations before channel-level LIVE_DATA validation. A successful HTTP
response alone is insufficient. Mock transports remain TEST_DATA and are rejected.
Provider failures record sanitized failure audits, not fabricated market observations.
Rate limits use persistent backoff; repeated failures open the existing circuit breaker.
Freshness and entitlement are checked again at receipt time, before archival, so a
request crossing either expiry boundary cannot store a qualifying market observation.

**Overall readiness stays NOT_READY and /signal stays NO_TRADE even when XAU becomes
validated.** Missing DXY, yields, calendar and news remain hard engine vetoes. No trading
logic is weakened to allow XAU-only trades. Champion stays Phase 3B; Challenger and
shadows remain isolated DEMO research with no automatic promotion. No broker/notifications.
Trading Economics, Finnhub and Forex Factory remain disabled and are never queried.

`/health`, `/system-status` and `/signal` (under readiness.xau) expose approval state,
boolean `credential_configured`, source/receipt timestamps, recomputed freshness,
last-attempt status and channel mode. Overall decision mode is never relabeled LIVE.
No key, key digest, raw provider response or raw provider exception is exposed.

## Environment names and exact activation sequence

Collection is OFF unless `MOBILE_COLLECTION_ENABLED` is exactly `true`; default is
`false`. Invalid toggle syntax fails startup. Approval is complete only with all of:

| Name | Required value / operator input |
| --- | --- |
| `MOBILE_XAU_APPROVAL_REF` | Operator approval reference; never the API key |
| `MOBILE_XAU_ENTITLEMENT_REF` | Verified subscription/entitlement reference |
| `MOBILE_XAU_ENTITLEMENT_UNTIL` | Future timezone-aware ISO 8601 expiry |
| `MOBILE_XAU_LIVE_ENTITLED` | `true`, only after confirming live entitlement |
| `MOBILE_XAU_SOURCE_TIMEZONE` | `UTC` |
| `MOBILE_XAU_UNIT` | `USD_per_troy_ounce` |
| `TWELVE_DATA_API_KEY` | Secret Render environment value only |

Existing operational names remain `MOBILE_DB_PATH`, `MOBILE_DISK_PATH`,
`MOBILE_MAX_DB_MIB`, `MOBILE_MIN_FREE_MIB`, `PORT`, `PYTHON_VERSION` and Render's platform
variables. No new polling-frequency control or provider URL override exists.

1. Obtain separately authorized deployment approval. Confirm the Twelve Data account
   actually licenses XAU/USD and every required interval, unit, real-time access and
   request volume. Do not manufacture approval references or entitlement expiry.
2. Stop the **new mobile-v2 service only** and preserve its existing database. Use the
   existing replay-verified archive procedure with its original configuration first.
   Never repurpose or delete the deployed database or touch the legacy Render service.
3. Deploy the reviewed implementation with collection `false`. Configure the complete
   approval fields, key via Render secrets, and a **new** `MOBILE_DB_PATH` such as
   `/var/data/mobile-v2-xau-epoch1.sqlite3` on the existing dedicated persistent disk.
   Provider approval changes alter immutable configuration identity; reusing the old
   database intentionally fails closed. Retain the old database separately.
4. Check `/health` and `/system-status`: collection false, NOT_READY, approved
   configuration, credential configured true, source unavailable pending validation.
5. Set only `MOBILE_COLLECTION_ENABLED=true` and restart the single worker. Keep all
   approval fields and the database path unchanged. Observe sanitized authentication,
   coverage, freshness and cadence checks. Do not treat approval configuration as proof
   of successful provider validation. No manual HTTP request triggers a collection tick.
6. Require at least three valid advancing observations and a successful restart/replay
   check. Confirm XAU health separately while overall readiness remains NOT_READY.
   Real account validation has not been performed by these offline tests.
7. Pause by setting the toggle false and restarting; retain approval fields and path.
   The operational toggle is deliberately excluded from engine identity so pause/resume
   does not invalidate replay. Approval/entitlement changes require a preserved new epoch.

## Storage and recovery limits

Existing 512 MiB database/WAL/SHM budget and 256 MiB free-disk reserve remain. The
scheduler stops before a cycle if less than 16 MiB of database-budget headroom remains,
and rechecks after a cycle. Responses and candle counts are bounded. No evidence pruning
or silent rollover occurs. Exhaustion requires operator archival/capacity review.
Original Phase 3F 192.84 MiB/day stress findings remain documented; that multi-provider
estimate is not a measured XAU-only rate. Measure actual XAU growth before unattended
operation and retain off-disk verified archives. This patch does not promise indefinite
collection within the existing disk budget.

Completed cycles replay on restart; duplicate minute buckets cannot create duplicate
evidence. Incomplete CAPTURED cycles still block startup for offline recovery review.
Missing acquisition intervals are recorded as gaps, not backfilled with invented data.
No automatic fallback to legacy Render exists.

## Verification and review

- Initial focused run after implementation: 59 passed, zero failures.
- Complete backend regression suite run once: 829 passed, zero failures.
- Final review then added receipt-time freshness/entitlement checks and two regression
  cases. All affected mobile tests were rerun: **61 passed, zero failed, zero skipped**.
  The full suite was not repeated after this localized fix; the unchanged engine and
  native provider code had passed the complete run. Both runs emit one existing
  Starlette/AnyIO deprecation warning.
- A first-receipt scheduler test initially expected a decision too soon. It was fixed
  to expect NO_DECISIONS_YET while cadence validation is pending; the production gate
  was retained. Three-observation tests verify both Champion and Challenger replay.
- Review added rejection of fixture data-status aliases, explicit delayed markers,
  excessive candle counts, secret reflection and validation expiry during transport.
- Tests exercise Render-safe lifespan startup, single scheduler start/shutdown,
  read-only GETs, disabled collection, missing/invalid keys, every missing approval
  field, expired entitlement, rate-limit backoff, future/stale data, forming candles,
  symbol/timezone errors, mock transport rejection, storage-budget stop, new-epoch
  enforcement and pause/restart persistence. All provider responses are offline tests;
  transport attestation simulations are explicitly test-only, not real-provider proof.
- No actual key was supplied, no real provider was contacted, and no Render settings
  were changed. Required provider approvals, real authentication/subscription validation,
  live cadence/restart evidence and measured XAU-only storage growth remain outstanding.
