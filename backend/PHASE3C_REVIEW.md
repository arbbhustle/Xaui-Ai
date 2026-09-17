# Phase 3C completion and forensic review

## Result

Final full backend suite: **420 passed, 0 failed**. This includes all **351**
pre-existing tests and **69** new Phase 3C regressions (49 core, 20 forensic).
One existing Starlette/AnyIO TestClient deprecation warning remains.

Command: `python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short`.
Tests use deterministic offline inputs and temporary SQLite databases. No live
provider performance or real-market promotion eligibility is implied.

Base HEAD remains `6ec0099057eac4134abe3248900499b8f3db9a0a`. Tracked-file diff and
staged diff are empty. All implementation/documentation files below are new and
unstaged. No Render operation, Android edit, broker integration, notification,
UI work, staging, commit, push or Phase 3D work was performed.

## Findings fixed during implementation and forensic review

| Finding | Correction and regression |
|---|---|
| Bounded recent-history selection could crowd purged older training records out | Query eligible training and recent guard windows separately before limiting; 250 recent records cannot evict 80 older eligible outcomes. |
| Malformed historical JSON could abort journal construction before recording a fallback | Avoid parsing JSON in the eligibility SQL; checksum/parser errors become a persisted CORRUPT_HISTORY decision. Champion still executes unchanged. |
| Last successful challenger action could remain visible after later companion failure | Response overlays latest health/replay block and forces NO_TRADE with zero effective influence. |
| Historical companion response did not enforce all current freshness checks | Reuse inherited macro/slow response guards, market candle ages, monitor heartbeat and expiry; historical record remains unchanged. |
| Learning evidence validation did not cover component votes, probability range or net accounting | Reject NaN/out-of-range evidence and inconsistent stored cost deductions; regressions fail closed. Future unavailable rows are ignored before their values are inspected. |
| Exact raw score 100 fell into a separate calibration bucket | Cap upper bucket at 4 so 100 belongs to the 80..100 bin. |
| Good aggregate promotion metrics could hide active drift or corrupt latest decision | Promotion replays/verifies the current decision and honors persistent replay block, current fallback and explicit drift guards. |
| Timestamp strings with different offsets could sort out of actual order | Sort effective exposures and performance by parsed UTC instants. |
| High hit rate with losing net expectancy could earn positive component trust | Nonpositive net expectancy caps learned strength at neutral or worse. |
| A vetoed result could ambiguously expose the influence used to form its proposed score | Persist proposed influence separately; effective influence is zero during fallback. |
| Importing source outcomes needed stronger integrity checks | Verify closing event equals final position and source snapshot hash matches; original champion decision integrity is checked. Integration regression confirms actual closed champion ingestion and first-known timestamp. |

## Causal and leakage audit

- Learning excludes OPEN trades, current-decision outcomes, unknown future
  results, future closes, wrong namespaces and overlapping feature/label windows.
  A future result's values/regime can be mutated without changing an earlier fold.
- Both close time and first-known time must precede the training cutoff and
  embargo. Delayed discovery cannot backfill knowledge into an earlier decision.
  Equality at the embargo boundary remains excluded.
- Actual longest closed input footprint (including 4H) is purged. Recent outcomes
  outside that training window are permitted only in a separate risk-reduction
  drift guard. They cannot train weights or calibration.
- Independent samples exclude overlapping exposures, preventing copies of one
  trade across cohorts from inflating effective evidence. Shadow trust uses only
  its own variant's closed outcomes; component trust uses actual aligned trades.
- All six entry-regime dimensions are isolated. Future or different-regime labels
  cannot increase current trust. Exact regime samples below minimum stay neutral.
- State-transition counts are reported before the current transition is added.
  Strict walk-forward folds reject out-of-order/equal decision timestamps and
  preserve earlier outputs when later labels are appended.
- Confidence is separate from heuristic scores, with source, sample IDs and
  shrinkage recorded. Missing calibration stays null. Brier/ECE use recorded
  probabilities before entry and retain gross versus net target distinction.
- Code, costs, policy and champion model identities define distinct namespaces.
  Replay uses captured inputs and normalized source identities, not current feeds.

These checks address known code-level leakage paths; they do not prove economic
causation or guarantee no statistical overfitting. Fixed thresholds, sparse joint
regimes, correlated components, observational credit and champion-proxy calibration
still require untouched point-in-time validation and forward DEMO results.

## Isolation and fallback audit

- Plain Phase 3B, companion-disabled and companion-enabled runs yield identical
  champion decisions and positions. Original source hash is enforced at startup.
- No meta outcome, adaptive weight, promotion status or hypothetical position
  feeds the champion evaluator, calibration or production position tables.
- Companion savepoint failure does not roll back champion work; generic logs do
  not expose injected exception content. Population of an original database is
  rejected without changing its history.
- Insufficient history, low confidence, uncertainty, data veto, drift, corrupted
  history or failed replay produces challenger NO_TRADE and zero influence.
  Champion remains the active route at all times, including challenger success.
- Replay survives process restart. Tampering is detected, persists a safety block
  and prevents later adaptive action. Hypothetical costs do not rewrite gross R.
- TEST_DATA/FIXTURE provenance is retained. Default real-mode policy rejects
  fixture inputs; all inherited Phase 3A/3B provenance regressions remain passing.
  No synthetic input becomes LIVE and no unavailable feed is fabricated.

## Promotion and operational status

Champion: **Phase 3B remains active and unchanged**.
Challenger: **separate DEMO hypothesis; never production-selected**.
Operational promotion: **PROMOTION_INELIGIBLE** — no validated real-provider
challenger outcome corpus has been supplied. Deterministic tests exercise both
report branches but do not constitute promotion evidence. Even eligibility only
requests human review; no promotion action exists.

## Costs, security and remaining blockers

Configured assumptions: spread **0.30** XAU/USD quote-price points round trip,
slippage **0.10 per side**, latency penalty **0.00**. Total default deduction
is **0.50 / initial risk points** R. They are documented simulated assumptions,
not measured broker costs; financing/commission and dynamic execution are absent.

Common credential/private-key/token patterns were scanned in all new source,
tests and documentation; no candidates were found. Manual inspection confirms
the existing key is read by environment-variable name only. SQL values are
parameterized, diagnostics omit exception contents, and no broker endpoint exists.
This is not a substitute for access controls before exposing the local service.

No known failing test or unresolved implementation blocker remains for the
requested offline Phase 3C scope. Operational prerequisites remain:

- Licensed, timestamped provider adapters/credentials from Phase 3A/3B: XAU/USD,
  USD/DXY, 2Y/10Y yields, calendar, news, optional Fed and slow-regime inputs.
  No additional provider is required by the adaptive layer itself.
- Sufficient point-in-time closed evidence and forward DEMO validation. Exact
  regime minima plus the conservative 4H purge impose a long warm-up. No warm-up
  relaxation or fabricated history was introduced.
- Storage optimization: **the inherited ~76 MiB / 1,440-tick concern remains open
  before deployment**. Repeated evidence in meta snapshots adds growth; historical
  analytics remain memory-based and source imports recheck bounded event sets.
  Plan archival/content-addressed storage, backup/restore and scaling without
  deleting replay evidence. Shared disk failure remains an infrastructure risk.
- Review calibration quality, proxy bias, model assumptions and simulated costs
  on data independent of implementation fixtures. Eligibility is not established.

## Exact files added

1. `backend/meta_council.py`
2. `backend/meta_evaluation.py`
3. `backend/meta_journal.py`
4. `backend/phase3c.py`
5. `backend/phase3c_main.py`
6. `backend/tests/test_phase3c.py`
7. `backend/tests/test_phase3c_forensic.py`
8. `backend/PHASE3C.md`
9. `backend/PHASE3C_REVIEW.md`

No existing tracked files were modified. Requirements/workflow already support
the added tests and were left unchanged. Both tracked ZIP files remain unchanged;
all **18** extracted Android source files byte-match the AURUM CORE v2 ZIP.
The pre-existing untracked Android directory is not part of Phase 3C. Ignored
runtime databases/caches and local artifacts were not staged or included.
