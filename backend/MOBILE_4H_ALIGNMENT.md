# Mobile Twelve Data 4h alignment compatibility v1

## Scope and root cause

Twelve Data's observed UTC 4h sequence can be 01:00/05:00/09:00/13:00/17:00/21:00.
UTC identifies the clock, not the bucket origin. Previous mobile aggregation used
`epoch - epoch % 14400`; the frozen shared validator also required epoch alignment.
Changing only aggregation therefore left native candles rejected during normalization.

The adapter derives the modulo offset from consistent closed native bootstrap bars.
There is no permanent 01:00 constant. Complete minute buckets use that observed
offset, preserving original timestamps and OHLC. Partial buckets are omitted, never
filled. Five requests bootstrap the process; normal steady-state cycles still make
one Twelve Data 1min request. Restart performs native bootstrap again.

## Explicit version and database epoch

`MOBILE_XAU_VALIDATOR` defaults to **`legacy`**, which preserves the previous engine,
normalization and database identity. The opt-in value **`mobile-twelve-xau-4h-v1`**
selects the compatibility engine. Unknown values fail startup. No Render variable has
been changed by this patch; collection remains independently OFF by default.

The version is recorded in mobile database identity, configuration identity, engine
identity and every compatible snapshot. Its source hash participates in the model/
forward identity. Use a separately preserved new database epoch when changing the
validator version. Existing databases are never rewritten, upgraded or reinterpreted.
To replay an old epoch, keep `legacy` and its original provider configuration. To
replay a v1 epoch, retain its version, exact code and configuration. The archive tool
uses the source epoch's engine class and verifies Champion and Challenger replay.

## Isolation and evidence

The unchanged frozen modules load into a private package namespace for v1. Only that
copy's `closed_frame` receives the scoped extension. Normal `backend.domain`, provider
normalizers and all legacy engines remain untouched, including Phase 3C's frozen-base
hash assertion. No global alignment check is weakened or normal module monkeypatched.

The extension applies only to 4h and only within a context tied to approved Twelve Data
XAU/USD evidence. 1min/5min/15min/1h delegate to the original validator. All existing
price, order, duplicate, close-time, continuity and freshness checks remain in force;
timestamps and the observation clock are not shifted to force acceptance.

Acquisition metadata archives an independent copy of 60–125 closed native bootstrap
bars, their request cutoff, response hash, provider identity, inferred offset and
compatibility version. Approval, live entitlement, USD-per-troy-ounce unit, UTC clock,
real transport and authenticated XAU/USD coverage remain required. Receipt-time
freshness and entitlement gates still apply. Tests simulate attestations offline;
they do not constitute real-provider approval or LIVE evidence.

V1 supports provider-proven whole-hour offsets modulo four hours: 0, 1, 2 or 3 hours.
Missing, mixed, unsupported, future or stale bootstrap evidence is rejected. A previous
archived receipt locks the offset for that provider epoch: even a consistent new offset
after restart is rejected. Automatic session/DST transition inference is intentionally
unsupported. Such a transition needs separate causal validation and a reviewed epoch.
One-minute responses alone cannot reveal an unannounced native-4h session change;
the fixed-anchor assumption must be confirmed with the provider before activation.

Replay restores validation context from the immutable capture selected at the decision
timestamp and requires exact equality with its recorded 4h input. It never consults a
current provider or process cache. Champion scoring stays Phase 3B; Challenger remains
isolated DEMO-only. Missing macro/calendar/news still enforce NOT_READY / NO_TRADE.

## Deployment boundary

This branch does not enable collection, change credentials, change Render settings,
deploy, merge to main or introduce broker/notification features. Future deployment
requires selecting the v1 configuration with a fresh persistent epoch, retaining old
archives, and completing real-provider approval/cadence/capacity checks. Existing
single-worker ownership, storage budgets, immutable evidence and fail-closed recovery
remain in place. Provider/session transitions remain an operational review requirement.

## Verified before branch commit

- 13 aggregation-anchor regressions and 29 scoped compatibility regressions passed.
- The three requested mobile collection/API/Phase 3F files passed all 151 tests.
- The complete backend suite passed 875 tests with zero failures (one existing
  Starlette/AnyIO deprecation warning).
- An initial focused failure exposed shared objects between frame data and bootstrap
  proof; the proof now uses an independent deep copy. Final tests include native
  acceptance, shorter-timeframe rejection, thread isolation, capture tampering,
  receipt-time expiry, derived bars, restart/archive replay and legacy identity.
