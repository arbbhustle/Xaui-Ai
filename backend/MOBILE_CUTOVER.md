# Parallel mobile backend cutover — preparation only

## Deployment target and architecture

Use **`backend.mobile.api:app`**, from the repository root. This is a new read-only HTTP boundary around the committed Phase 3F store/engine, not the old Phase 1 `backend.main:app` or the local-only Phase 3C application.

The unchanged engine chain is `RealEngine` (Phase 3F) → `ForwardEngine` (Phase 3E) → `Phase3CEngine` (isolated research Challenger) → `Phase3BEngine` (production DEMO Champion, hidden-state/DGFE) → Phase 3A intelligence → Phase 2 council. Phase 3D remains an offline evaluation lab: its research safeguards do not become a prediction source or promote a strategy. Champion remains Phase 3B. Challenger and shadows remain research-only; automatic promotion is impossible in this service.

The existing Phase 3E/3F runners deliberately refuse Render. Their guards, source hashes and replay logic are unchanged. The new runtime initializes the same store/engine classes and verifies journal/replay integrity, but **does not create a polling worker, adapters, fixture generator or execution loop**. It accepts no public mutation endpoints. It is deployment-ready in the requested **collection-disabled integration mode**, not ready for live market intelligence or Forward DEMO trading evidence.

## API contract

| Route | Contract |
| --- | --- |
| `GET /health` | Process/storage health. HTTP 200 with readiness NOT_READY is expected when collection is disabled; storage failure/budget exhaustion returns 503. |
| `GET /signal` | Flat backward-compatible Champion fields plus a `champion` object. Matching `challenger` research is separate. Missing data yields NO_TRADE, null confidence, no invented prices/scores. |
| `GET /performance` | Provenance-qualified Phase 3F report, separated Champion/Challenger/shadow cohorts. Insufficient cohorts omit metrics and show INSUFFICIENT_FORWARD_DATA. |
| `GET /history` | `items`, `next_cursor`, source; default 30/max 200. Archived Champion decisions retain available legacy/council/intelligence details and explicit data mode. |
| `GET /trades` | `items`, `next_cursor`; immutable finalized Champion demo outcomes only, default 50/max 200. Research outcomes cannot join Champion history. Gross/net R and cost/excursion fields appear only if recorded. |
| `GET /system-status` | NOT_READY, collection disabled, candidate providers UNAPPROVED/UNCONFIGURED, Forex Factory secondary-only/disabled, storage budget and startup replay audit. |

`before_row` accepts positive cursors on history/trades. No endpoint changes stored history. Responses use no-store headers. Failures return sanitized NO_TRADE/UNAVAILABLE responses, never old active signals or raw exception/credential text.

Available signal details include technical components, momentum, structure, volatility, alignment, hidden state/DGFE/resilience/liquidity/entropy/absorption, macro/news/event risk, regime/session, explanations, vetoes, observed minute price and source timestamps. There is no invented data when these fields are missing. Archived fields are not a newly computed current trading decision: the disabled service applies a response-only NO_TRADE veto and clears actionable levels. It never rewrites the historical decision.

Freshness is calculated from source candle/observation timestamps on each request, separately from API response time. Future decisions/finalizations are excluded. Synthetic flags and TEST/FIXTURE inputs remain TEST_DATA, even after becoming stale. Collection-disabled readiness never advances and the service never issues a LIVE_DATA badge. Calendar/news revision and feature causality remain governed by the unchanged Phase 3F engine and its regression suite.

## New Render service only

Blueprint file: **`deploy/render-mobile-v2.yaml`**. Import it as a **new** service named `dardania-xautrade-ai-v2`. Do not attach it to `xau-ai-trader-android`; do not modify that service's configuration, disk, environment or URL. Automatic deployment is off. No deployment has been performed.

Repository-root build command:

```sh
pip install -r backend/requirements.txt
```

Start command:

```sh
python -m uvicorn backend.mobile.api:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log
```

Runtime: Python 3.12.8 and the existing pinned Phase 3F runtime requirements. One process/instance; a process lock rejects a second owner. Health-check path **`/health`**. Paid persistent disk: **2 GiB**, mounted at `/var/data`; SQLite path `/var/data/mobile-v2.sqlite3`. Render startup fails if the database is not on the mounted disk. It also rejects an existing non-mobile database and the legacy service name/path. Do not reuse any Phase 1–3F experimental or legacy database.

Environment variable **names** for this deployment:

- `PYTHON_VERSION`
- `PORT` (provided by Render)
- `MOBILE_DB_PATH`
- `MOBILE_DISK_PATH`
- `MOBILE_COLLECTION_ENABLED`
- `MOBILE_MAX_DB_MIB`
- `MOBILE_MIN_FREE_MIB`
- `RENDER`, `RENDER_SERVICE_NAME` (platform-provided safety context)

Non-secret defaults are in the Blueprint. `MOBILE_COLLECTION_ENABLED` must remain `false`; any enabling value fails startup. Credentials are **not required** and must not be supplied for this initial integration deployment. Existing adapter credential names for a later separately approved activation are `TWELVE_DATA_API_KEY`, `TRADING_ECONOMICS_API_KEY`, `FINNHUB_API_KEY`. These names do not imply provider approval or sufficient subscriptions. Forex Factory additionally needs validated usage authorization; it never becomes the primary calendar authority. Environment credentials alone cannot activate this service.

Configuration follows [Render Blueprint documentation](https://render.com/docs/blueprint-spec), [persistent disk requirements](https://render.com/docs/disks), and [Python version selection](https://render.com/docs/python-version). Service-name/plan availability and the final public URL must be confirmed when deployment is separately authorized. Disk attachment entails maintenance downtime; this is not a multi-host SQLite configuration.

## Storage and retention

Initial collection-disabled service: no minute ticks, receipts, snapshots or decision records are generated by GETs. **Incremental market/evidence storage is 0 MiB/day and 0 MiB/30 days**, apart from the initial small SQLite schema and fixed startup metadata. Host logs/Render disk snapshots are separate; access logs are disabled. Repeated-read tests compare complete logical database dumps to verify zero evidence mutation.

Do not apply this zero-growth estimate to future collection. The Phase 3F offline native-shaped stress result remains **192.84 MiB/day, 5,785.32 MiB per 30 days (~5.65 GiB)** for the complete database, excluding WAL/SHM, backups and filesystem overhead. It is a test-payload extrapolation, not a measured real-provider forecast. See `PHASE3F_STORAGE.md`.

Existing shared provider-object deduplication and compressed replay snapshots are retained. This service adds a **512 MiB active database/WAL/SHM budget** and **256 MiB free-space reserve**. Budget violations fail startup or make health degraded; they do not delete evidence. At the old stress rate, 512 MiB is roughly 2.65 days of acquisition, demonstrating why future collection cannot simply be switched on. Live activation requires longer capacity profiling and an explicitly reviewed archival/retention workflow.

No automatic deletion, downsampling, replay-breaking TTL, or silent database rotation is implemented. The safe retention boundary is to stop acquisition (already disabled), preserve the complete epoch, verify the archive, and require deliberate off-disk archival before any future new epoch. Audit archives must retain their original engine/configuration for replay.

Offline complete archive (stop the API so its process lock is released):

```sh
python -B -m backend.mobile.archive --database /var/data/mobile-v2.sqlite3 --output /var/data/archive-approved-epoch.sqlite3
```

The command checks destination headroom, uses SQLite online backup (including WAL), verifies hashes and exact Champion/Challenger replay, and atomically publishes a new archive without overwriting an existing file. It never prunes the source. Move verified archives to separately managed durable storage under an explicit operational policy; do not accumulate unlimited backups on the service disk. Hard-link publication requires a supported local filesystem and otherwise fails closed.

## Startup/restart and recovery

On startup: require a dedicated database, acquire its single-owner lock, initialize/validate schema, verify immutable evidence, match engine/config identity and replay existing decisions. No missing market intervals are fabricated. An incomplete CAPTURED cycle causes a fail-closed startup requiring offline recovery review; it is not silently discarded or replayed as a new current signal. This deployment cannot create pending acquisition cycles because collection is absent. A corrupted database must be investigated/restored, never replaced automatically with an empty database.

## Android cutover preparation

The default remains `https://xau-ai-trader-android.onrender.com/signal`. The existing Settings endpoint editor now accepts either an HTTPS base URL or the full `/signal` endpoint, persists the normalized endpoint and cancels the previous in-flight request. No screen redesign or provider keys.

After the new service is separately deployed and its actual URL verified, paste that HTTPS base URL into Settings → Backend Connection → Save endpoint & sync. To roll back, paste the legacy base URL. **No assumed future Render hostname is baked into Android.** No automatic fallback between services: a failed new-service request stays visibly unavailable instead of silently presenting legacy information.

## Local verification commands

```sh
python -B -m pytest backend/tests -q -p no:cacheprovider
python -B -m backend.tests.mobile_http_probe --output <new-temporary-directory>
```

The test-only HTTP probe starts Uvicorn on loopback, exercises all six endpoints against empty and explicitly TEST_DATA seeded databases, then stops the servers. It exports temporary JSON for Android contract tests; these are not repository artifacts or runtime fixtures. Set `MOBILE_API_CONTRACT_DIR` to that output directory while running Android `assembleDebug testDebugUnitTest lintDebug`. The production Android model parses those real HTTP responses; without the environment variable the optional integration test is explicitly skipped, not silently reported as exercised.

## Limits before market operation

This is ready only for a separate **disabled-data integration service** after deployment approval. Real provider approval, credentials, authenticated entitlements, exact symbol mappings, live freshness/cadence validation, calendar coverage, operational collection activation review and sustained forward evidence remain absent. NOT_READY and no predictive-edge/profitability claim remain correct. Storage profiling/retention qualification is required before collection activation. No Phase 4B notifications, broker execution or Challenger promotion.

## Final local verification (2026-09-19)

- Complete backend suite: **800 passed, 0 failed**, including 30 mobile boundary regressions. One existing Starlette/AnyIO deprecation warning.
- Android debug build: **successful**. Unit/contract tests: **79 passed, 0 failed, 0 skipped**. Lint: **0 errors, 25 warnings**.
- All six endpoints returned HTTP 200 through a real loopback Uvicorn server in both empty and explicitly TEST_DATA scenarios (12 endpoint checks). The Android production parser consumed those exported HTTP responses with the integration test enabled.
- Restart and complete-archive regression tests verified exact Champion and Challenger replay. Empty stores report NO_DECISIONS_YET rather than inventing replay evidence. Repeated API reads do not mutate evidence.
- The empty local database measured **258,048 bytes (~0.246 MiB)** after shutdown; the small test-seeded database measured 323,584 bytes. These are initialization/contract-test measurements, not a replacement for the Phase 3F daily stress estimate above.
- Review covered fixture provenance on finalized outcomes, future decision/finalization exclusion, per-source freshness, missing provenance, separated research outcomes, fail-closed corruption/budget handling, secret-safe errors and disabled collection. No actual credentials were added.
- A full-suite failure exposed an archive SQLite handle left open on Windows. The archive now explicitly closes its backup connection, verifies without reinitializing the copied database, and publishes exclusively using a hard link. Regression coverage disables garbage collection to ensure cleanup does not depend on it.
- Android base URLs now normalize to `/signal`; the legacy default and HTTPS-only validation remain intact. No screen redesign, engine logic change or provider activation was made.

Intended change set (11 files): `backend/mobile/__init__.py`, `backend/mobile/runtime.py`, `backend/mobile/api.py`, `backend/mobile/archive.py`, `backend/tests/test_mobile_api.py`, `backend/tests/mobile_http_probe.py`, `backend/MOBILE_CUTOVER.md`, `deploy/render-mobile-v2.yaml`, `XAU_AI_Trader_Android/app/src/main/java/com/arbnor/xauai/ApiClient.kt`, `XAU_AI_Trader_Android/app/src/main/java/com/arbnor/xauai/MainActivity.kt`, and `XAU_AI_Trader_Android/app/src/test/java/com/arbnor/xauai/MobileIntegrationTest.kt`.

Nothing was staged, committed or deployed. The existing Render service was not contacted or changed. The actual new Render URL, platform provisioning and device-to-new-service connectivity remain post-deployment checks; no remote deployment is claimed by these local results.
