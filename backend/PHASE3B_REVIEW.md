# Phase 3B implementation and final forensic review

Base commit: `f436e9c3c990d263bfdc46fae63a56eeaec1e9a5`.
Work resumed from the existing local implementation; it was not restarted.
Scope remains local DEMO Phase 3B. Nothing staged, committed or deployed.

## Verification

Final complete suite: **351 passed**, including **90 Phase 3B cases** across
`test_phase3b.py` and `test_phase3b_forensic.py`; all prior 261 tests remain passing.
One existing Starlette/AnyIO TestClient deprecation warning remains.

Command: `python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short`.
All tests use deterministic synthetic inputs and temporary SQLite databases.
No provider network call, Render deployment or Android modification was required.

## Bugs and review corrections

| Finding | Fix and regression coverage |
|---|---|
| A prior sweep remained active after subsequent breakout invalidated the rejection | Require the latest close to remain reclaimed on the relevant side; tolerate float noise at a level. Regression checks invalidation. |
| Shadow-journal failure could roll back the production transaction | Savepoint rollback isolates shadow writes; injected post-write failure preserves the production decision/position and emits no exception content. |
| Event impulse could use a pre-event close separated from release by a gap | Require contiguous pre-event baseline reaching the exact release boundary; incomplete windows remain unresolved. |
| Known pre-event macro pressure was discarded during an event blackout | Event-risk vetoes no longer erase otherwise valid pressure evidence; stale/conflicting/future source vetoes still do. No later observation can substitute for pre-event pressure. |
| 1m shock could remain invisible until a 5m shock bar | Added explicit 1m baseline shock detection and a before-next-5m regression. |
| Scheduled weekend gap could strand an expired shadow order | Recognize only scheduled market closures using the existing session policy; arbitrary gaps remain unresolved. |
| Sustained directional expansion fell through to RANGE without a new explosive bar | Add an efficiency/bias continuation path, disabled during compression; regression checks a persistent directional trend. |
| Slow-source failures could retain a LIVE-style response status | Mark slow-regime-blocked decisions/responses explicitly and recompute freshness on every response. |
| Synthetic envelope marker could be ignored | Reproduced failing regression; restrictive envelope provenance now overrides LIVE labels. |
| TEST_DATA macro/news record marker could be stripped by Phase 3A normalization | Reproduced failing regression; TEST_DATA is preserved as synthetic FIXTURE provenance, with explicit adapter/envelope/record checks. |
| Market wrapper could overwrite a marked FIXTURE/TEST_DATA payload with adapter LIVE | Both marker variants reproduced failures; collection now preserves restrictive payload provenance. |
| Non-boolean synthetic markers could be silently ignored | Reject malformed marker types and unknown explicit record modes. |
| Resilience could compare a session-spanning move to one-hour pressure or ignore a configured horizon | Require contiguous 1m gold history matching the configured macro momentum window; missing aligned history produces null. |
| Unstable-state threshold allowed an unreachable value | Limit configuration to 2..3 transitions, consistent with the captured three prior states; invalid-boundary regression. |
| Analytics cohorts could be read at different commit instants | Read production and shadow cohorts in one SQLite read transaction. |

Traceability improvements made during review: all three shadow evaluations are
journalled, including skips; per-driver resilience remains visible; pre-event
expectations expose their observation time and originating snapshot ID.

## Look-ahead and leakage audit

- Closed-candle validation is inherited; forming OHLC cannot affect features.
  Future gold candles veto all five timeframe paths. Higher-timeframe features
  stay within the 5m entry cutoff; 1m timing uses the decision observation time.
- Trailing swing extrema exclude the candidate/reaction bars; no future-confirmed
  pivot or retrospective state label is used.
- Slow inputs enforce observation <= publication <= receipt <= decision time.
  COT publication lag is explicit. Latest observation/publication ages are checked
  independently; polling cannot refresh an old economic observation.
- Event initial impulse uses only completed post-event minutes available now.
  Recovery cannot use a future bar. Missing pre/post history remains unresolved.
  Failed expected reaction requires pressure recorded before the release, with
  no forward fill from present-day expectations.
- State history and prior pressure are captured before the current decision;
  future history entries are excluded. Later provider revisions cannot alter the
  replay of an earlier decision. Decision and snapshot checksum regressions pass.
- Resilience is a heuristic contemporaneous residual, not a causal estimate or
  forecast probability. Missing observations stay null; session gaps do not get
  disguised as a one-hour response.
- The same source normalization applies before snapshot hashing. Malformed or
  stale slow data blocks entries but does not freeze valid existing 1m exits.

## Shadow isolation audit

- Separate positions, evaluations, events and per-variant unique-active constraints.
- Production calibration queries only production `trades` and `trade_events`.
  Injected highly profitable shadow outcomes do not increase its sample count.
- Enabled-versus-disabled shadow runs produce identical production decisions and
  positions; no shadow score, outcome or occupancy enters the council input.
- Injected shadow failure rolls back only shadow work. The main decision remains
  replayable. Exceptions are replaced by a generic failure code.
- Fill and outcome checks cover next full minute, absent minutes, provenance
  mismatch, same-bar stop/target ambiguity, restart persistence and session gaps.
- Physical database/disk failures remain shared-service operational risks. Logical
  isolation is not a promise of separate infrastructure or unlimited disk space.

## TEST_DATA isolation audit

- Explicit test opt-in is required; TEST_DATA slow data also requires fixture gold.
- Adapter, envelope, record and market-payload synthetic markers cannot promote
  to LIVE. Malformed explicit markers fail closed. Macro aliases become FIXTURE.
- Test/live mode assignments enter model identity; existing Phase 3A fixture/real
  database and position guards remain. Current API policy cannot be relaxed by a
  historical decision's test permission.
- Sources that are unavailable remain unavailable. Synthetic examples are in
  tests, never an automatic real-service fallback.
- Honest source labeling is a trust boundary: a malicious trusted adapter that
  strips every marker cannot be distinguished from truthful data by this contract.

## Security, performance and remaining blockers

Changed source/tests were scanned for common secret/private-key/token patterns;
no credential candidates were found. Provider errors log generic codes, source
URLs reject credentials, SQL values use parameters, and there are no trading or
public custom-data mutation endpoints. The application is intended for local use.

Initial Phase 3B fixture tick measured approximately 74 ms locally. Snapshot
payload was 56,737 bytes (~77.92 MiB per 1,440 ticks); this was an initial snapshot
without accumulated history and is not a production benchmark. Subsequent source
changes and real provider payload sizes vary. Decision/trade/shadow/event records,
context, WAL and indexes add to growth. **The inherited ~76 MiB/1,440-tick concern
remains an explicit pre-deployment optimization item**, not a closed finding.

No known unresolved implementation or test blocker for the requested offline
Phase 3B scope. Before real-provider DEMO activation: implement and validate
licensed XAU/USD, USD/DXY, 2Y/10Y yields, calendar, news, optional Fed, COT, ETF,
physical and options/skew/crowding adapters; supply required credentials securely;
validate models against point-in-time history and forward DEMO results; plan
storage, backups, retention preserving replay, analytics scale and access controls.
The existing Twelve Data key was absent in the checked environment. Public COT
data may need no key; other credentials/entitlements depend on chosen providers.

## Exact local files changed

Modified compatibility/documentation/workflow files:

- `.github/workflows/backend-tests.yml`
- `backend/README.md`
- `backend/engine.py`
- `backend/intelligence_providers.py`
- `backend/main.py`
- `backend/manage.py`
- `backend/phase3a.py`

New files:

- `backend/gold_analytics.py`
- `backend/gold_shadows.py`
- `backend/hidden_state.py`
- `backend/phase3b.py`
- `backend/phase3b_main.py`
- `backend/slow_context.py`
- `backend/tests/test_phase3b.py`
- `backend/tests/test_phase3b_forensic.py`
- `backend/PHASE3B.md`
- `backend/PHASE3B_REVIEW.md`

Requirements unchanged. Android's 18 extracted source files still match the
original AURUM CORE ZIP. Both tracked ZIP files are unchanged. The pre-existing
untracked Android directory remains excluded. No databases or local measurement
artifacts were added to the repository, and the staging area remains empty.
