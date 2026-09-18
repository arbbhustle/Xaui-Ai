# Phase 3E completion and forensic review

## Scope and state

Phase 3E adds a local forward-DEMO service, approved provider contracts/adapters,
strict provenance, causal archives, immutable evidence, restart-safe collection,
shared compressed storage, and readiness checks. The committed Phase 3D base
`bcafa19874bc26b77568a6ad1e08024b4dc325fc` is unchanged.

**Actual readiness is NOT_READY.** No real feed was configured or contacted. No
real-provider credentials or forward-trading evidence were available. Contract
mocks exercise the LIVE-labelled input path only inside tests; they are not a
live dataset and are never imported by the service.

Champion remains Phase 3B. Phase 3C Challenger is isolated, DEMO-only and cannot
auto-promote. Research remains INSUFFICIENT_EVIDENCE, promotion remains
PROMOTION_INELIGIBLE, and no 75% win-rate or predictive-edge claim is established.
No staging, commit, deployment, Android change, broker trading or Phase 3F work.

## Bugs found and corrected

These were defects or unsafe gaps found while implementing/reviewing Phase 3E;
the earlier committed engine was not edited.

1. **Nested provenance and delay:** nested synthetic/data-mode/delay markers
   could be discarded by adapter normalization. They now restrict the whole
   envelope before whitelisting; historical inputs cannot become LIVE.
2. **Timestamp validation:** source/receipt inversions, future record publication,
   naive timestamps and non-UTC candle representations needed independent checks.
   Internal candle timestamps now normalize to UTC; malformed higher frames do
   not invalidate otherwise valid 1m data.
3. **Fresh wrappers over stale sources:** recent envelope stamps could hide stale
   contributing slow or market observations. Source timestamp consistency and
   per-source latest-observation freshness now gate selection.
4. **Observation identity collisions:** the same numeric candles/revision could
   share an identity across different modes or raw payloads. Identity now includes
   mode, publication and raw hash, preserving provenance changes explicitly.
5. **Reused revision detection:** checking only the latest edition could miss an
   earlier reused revision. All matching source/observation/revision editions are
   checked before accepting the normalized content.
6. **Receipt-specific recovery:** recovering an old edition's original health
   could lose a later out-of-order veto. Each receipt references its own immutable
   acquisition document; exact recovery retains health, errors and receipt time.
7. **Interrupted acquisition:** a crash after durable provider receipts but before
   the capture could cause unnecessary refetch or an unreported lost minute.
   Same-cycle receipts now recover without refetch; later-minute recovery records
   an explicit ACQUISITION_GAP and does not backdate a decision.
8. **Exit monitoring:** missing higher frames and old irrelevant minute gaps
   could unnecessarily block exits. Independently valid minute bars remain usable;
   the existing trade checkpoint handles relevant continuity. A partial secondary
   source can still veto a conflicting minute price.
9. **Native adapter bounds and throttling:** higher-frame failures originally
   discarded valid minute data, nested worker calls could remain non-daemon, and
   partial-frame HTTP 429 handling could lose Retry-After. Bounded per-frame
   workers preserve partial data; rate-limit errors retain their delay and gate
   collection conservatively.
10. **Champion isolation:** a Challenger replay/journal failure could feed a shared
    risk gate and suppress the Champion. Companion health/replay controls are now
    separate. Tests compare identical Champion results with a broken Challenger;
    full forward-evidence readiness remains blocked when companion evidence fails.
11. **Storage duplication:** copies of the same provider data appeared in archive,
    capture, selection and health views. Shared content-addressed references now
    preserve exact reconstruction with one normalized-value object. Companion
    inputs/results and native snapshots are compressed transparently.
12. **Integrity and audit cost:** full-history scans on every tick grew quadratically,
    while checkpoint references and compressed companion checksums needed explicit
    verification. Full startup/status audits check references and native journal
    checksums; normal ticks incrementally check new immutable objects/snapshots.
13. **Evidence identities:** NO_TRADE monitor records without a new 5m signal lacked
    a decision identifier. They now have stable ledger decision IDs, with the
    original engine decision ID kept separately.
14. **Writer lock ownership:** a concurrent `once()` could replace a scheduler's
    lock handle. Each one-shot call now owns a separate lock object and fails
    cleanly while another writer holds the database.

Regression coverage also verifies append-only gross/net outcomes, single-history
recovery after transaction rollback, corrupt compressed payload rejection,
immutable SQL tables, cold backup restore, source conflicts, DST transitions,
timeouts, circuit breakers, rate limits, and secret suppression.

## Forensic conclusions

- **Look-ahead / leakage:** decisions use closed candles and observations received
  by the capture time. Future publication/observation is rejected. Future scheduled
  calendar events remain legitimate risk inputs. Revised data is a new as-received
  edition; it cannot rewrite past captures. Outcome evidence is appended at first
  knowledge, separately from predictions. Existing causal learning/embargo tests
  remain part of the complete suite.
- **Mode integrity:** TEST_DATA, FIXTURE, delayed, historical and unavailable feeds
  cannot be selected as fresh real forward inputs. Source approval/entitlement is
  still an operator attestation, not independent proof that a vendor is truthful.
- **Champion/Challenger:** routing and underlying scoring stay unchanged. The new
  forward layer only adds provider/integrity entry vetoes. Challenger failures
  affect companion evidence readiness, not Champion scoring or promotion.
- **Crash safety:** capture precedes evaluation; decisions, simulation changes,
  evidence and checkpoint commit atomically. A missing market interval stays a
  visible gap; no prices, fills or earlier acquisition times are invented.
- **Secrets:** source/docs/test pattern scan found no secret values. Tests use
  generated disposable sentinels to verify header-only authentication and archive
  exclusion. Raw responses and exception strings are not persisted or logged.
- **Storage integrity:** hashes and immutable-table triggers provide local tamper
  evidence, not protection against an administrator who rewrites the entire store.
  Real host permissions, independent backups and restore drills remain necessary.

## Storage measurements

Disposable **60-tick, one-minute offline contract sample**, six critical providers,
with snapshots, acquisitions, selections, health, Champion/Challenger evidence,
shadow records and engine journals. This is an engineering storage sample, not
market-performance evidence.

| Measurement | Measured sample | Linear estimate / 1,440 ticks |
|---|---:|---:|
| Same snapshots as canonical uncompressed JSON | 3,750,077 bytes | 85.832 MiB |
| Compressed immutable snapshots | 409,135 bytes | 9.364 MiB |
| Shared compressed forward objects | 1,002,535 bytes; 1,260 objects | 22.95 MiB payload |
| Whole SQLite database, including all tables/indexes | 3,452,928 allocated bytes | **79.031 MiB** |

The earlier Phase 3D sample measured 82.82 MiB/day of raw snapshots and roughly
105.47 MiB/day of database pages. The first Phase 3E prototype, before shared
provider references and companion compression, measured **106.312 MiB/day** of
database pages. The final shared-reference design reduces that prototype's whole
database estimate by about **26%**; snapshot payloads fall by about **89%**.
Small source-hash changes can change compressed byte counts slightly.

**Do not confuse snapshot savings with total database growth.** Snapshots are
substantially below the inherited ~76–80 MiB target; the complete evidence store
is still around 79 MiB/day on this short sample. This is not a production capacity
promise and is not substantially below 80 MiB/day for the entire database.

Exact sample checks: **60 captures reconstructed, 12/12 Champion decisions
replayed, 12/12 Challenger decisions replayed, 120 ledger entries verified.**
Separate regression tests compare plain/compressed engine results, shared-capture
round trips, and both replays after SQLite backup/restore.

### Storage choices evaluated

- Whole-snapshot compression is retained. A fine-grained shared candle/tree
  prototype on this sample used 1,678,814 bytes plus its included root index,
  roughly 38.43 MiB/day, compared with ~9.36 MiB/day for whole snapshots. More
  nodes/reference strings performed worse on this short corpus.
- Shared provider/macro/news value references are implemented inside SQLite.
  Selection and health views reuse observation documents. Receipt-specific
  provenance remains separate rather than being deduplicated away.
- Companion decision inputs/results use the same transparent bounded compression.
  Native JSON-query-dependent trade/decision tables are not blindly compressed.
- Retention tiers remain conservative: retain all reachable evidence in the local
  operational archive. A future cold tier must seal a manifest, preserve every
  referenced object/source version, and pass restore plus full replay before any
  pruning. No retention deletion or live-data migration is implemented.

The inherited storage-growth concern remains a **pre-deployment optimization and
capacity-validation item**. Estimates exclude WAL/SHM, filesystem blocks, backups,
large real news feeds and mature learning-history growth. Full status/startup
audits remain proportional to retained history. Real multi-day operation and
restore/retention validation are still required before deployment.

## Tests and remaining blockers

Final full suite: **628 passed, 0 failed**, in 83.82 seconds. This includes the
505 existing tests and 123 Phase 3E cases. There is one existing third-party
Starlette/AnyIO deprecation warning. The final source/docs/test scan covered all
12 Phase 3E files and found no secret-pattern matches. Tracked and staged Git
diffs are empty; only the listed new Phase 3E files and the pre-existing untracked
Android directory remain. Run from the repository root with the existing backend environment:

```text
python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short
```

No unresolved implementation defect is currently identified by the completed
regression coverage and review. Operational readiness is blocked by:

1. No approved/credentialed real XAU, DXY, 2Y, 10Y, calendar or news feed.
2. Actual native Twelve Data UTC metadata, intraday entitlement and rate limits
   still need vendor-account verification; generic gateways need schema mapping,
   licensing, canonical units, event coverage and source-edition validation.
3. Optional Fed, COT, ETF, physical and options contexts require real approved
   sources and environment credentials if enabled.
4. No sustained, independently cross-checked real forward captures or mature
   forward outcome/calibration evidence; no promotion eligibility is established.
5. The capacity/retention/backups item above remains open before deployment.

## Exact files added

- `backend/forward/__init__.py`
- `backend/forward/__main__.py`
- `backend/forward/contracts.py`
- `backend/forward/providers.py`
- `backend/forward/collection.py`
- `backend/forward/store.py`
- `backend/forward/runner.py`
- `backend/forward/profile.py`
- `backend/tests/test_phase3e.py`
- `backend/tests/test_phase3e_forensic.py`
- `backend/PHASE3E.md`
- `backend/PHASE3E_REVIEW.md`

No tracked baseline file was changed. The pre-existing untracked
`XAU_AI_Trader_Android/` directory is outside these changes and was not touched.
