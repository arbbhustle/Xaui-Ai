# Phase 3D final forensic review

## Scope and verification

Base remains Phase 3C commit `0bbfe2e162857064eae562dfe243521cb992fd1d`.
Phase 3D is additive offline research code. Production champion remains Phase 3B;
Phase 3C remains an isolated DEMO challenger with no auto-promotion.

Final complete suite: **505 passed, 0 failed** — all prior **420** cases plus
**85** Phase 3D cases (57 core and 28 forensic). One existing Starlette/AnyIO
TestClient deprecation warning remains.

Command: `python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short`.
Tests and measurement runs use labelled fixtures and disposable temporary stores.
Additional fixture runs covered the 23 benchmark/ablation variants and all 24
Champion/Challenger robustness combinations. No real-market inference is made.

## Bugs and implementation corrections

| Finding | Fix / regression evidence |
|---|---|
| Validation integrity inspection could consume future holdout captures | Restrict validation/model inputs to the pre-holdout prefix. Mutating valid JSON price values in holdout leaves earlier results unchanged. Full archive hash stays frozen separately. |
| Warm-up could generate learning outcomes | Explicit research warm-up veto plus no shadow/meta execution; prior macro context still warms. Regression confirms all position/outcome tables stay empty. |
| Legacy technical/baseline results could retain LIVE_DATA on fixtures | Research-only provenance annotation and FIXTURE_DATA label; production source unchanged. |
| Registry SQLite contexts committed but did not close Windows handles | Explicit context-manager close in finally. Reproduced cleanup failure, regression and subsequent disposable-store cleanup passed. |
| Duplicate or unrelated terminal entries could manufacture a successful validation | Completion must match one RUNNING start/manifest and can occur only once. Failed attempts remain visible. |
| Calibration overconfidence counted individual forecast errors instead of calibration gaps | Weight positive/negative bin gaps; a correctly calibrated .75 fixture now yields zero over/underconfidence. |
| Exposure omitted still-open or cross-boundary positions | Report clipped observed exposure independently of closed-outcome eligibility, including cohort exposure. Never invent liquidation returns. |
| Delayed challenger counterfactuals could inherit the wrong probability target | Carry explicit NET versus GROSS targets into execution-only records. Deduct costs once from gross R. |
| Shadow lookup assumed decision ID existed inside the evaluation payload | Read its relational decision_id column; complete shadow scorecards/stresses execute. |
| Shadow delay stress initially left shadow execution unchanged | Dedicated counterfactual executors apply the same delay/drift scenarios and label lack of learning feedback. |
| Archive profiling counted unrelated pre-existing objects | Traverse only roots reachable from the measured corpus. Adding an unrelated object cannot change profile totals. |
| Oversized compressed objects could be written but rejected on reconstruction | Bound individual objects and chunk large documents; large-document round-trip regression passes. |
| Replay fingerprint inherited incomplete older source lists | Cover every backend/research Python helper, requirements and runtime versions, including news journalling. Helper/dependency changes invalidate identity. |

A conflict-test fixture initially used words outside the existing sentiment
lexicon. It was corrected to recognized opposing headlines; production sentiment
rules were not expanded or tuned to satisfy a test or win-rate target.

## Look-ahead, leakage and holdout attack results

- Future/forming candles, naive timestamps, future news/macro availability,
  mismatched capture times, hidden synthetic provenance and silent OHLC revisions
  are rejected. Missing/duplicate/stale data remains explicitly flagged.
- Revision reception is mandatory when an existing source ID changes. Earlier
  captured editions remain unchanged; final values are not backfilled.
- Source ordering changes yield equivalent normalized input. Duplicate news and
  conflicting providers remain visible or vetoed by existing normalizers.
- Every model gets only the current as-received capture. Each has a separate
  database. The independent reference classifier only supplies contemporaneous
  cohort labels; it never alters candidate scores or entries.
- Calibration uses both economic close and known time before the feature footprint
  and embargo. Warm-up is not training, same/open/future outcomes are excluded,
  and cross-boundary outcomes are purged from scoring without fake endpoint exits.
- Holdout requires the same completed validation manifest. Changed costs/data/
  settings cannot access it through that plan. First access locks all later runs
  for that dataset, including after failure. Sensitivity has no holdout route.
- Failed/rejected attempts and all variant counts remain in the hash-chained
  registry. There is no automatic parameter selection, favorable-regime filtering
  or 75%-target optimization. Exact replay uses frozen data, configuration and code.

No known unclosed implementation-level leakage finding remains in the tested
contract. This is not proof of provider truth or absence of statistical overfitting.
Local registry governance can be bypassed by deliberate deletion/renaming/new lab;
real experiments need independent custody and review. Dependence, selection bias,
multiple testing and small samples remain explicitly limited, not dismissed.

## Costs, uncertainty and model isolation

- Original production source and hashes remain unchanged. Ablations use copied
  function globals; common module weights are never mutated. New research wrappers
  and purges are explicitly distinguished from production behavior.
- No research code writes a production database, changes routing, deploys a
  service or promotes a challenger. Native engine databases are temporary and
  independent. Execution stresses are counterfactual, not feedback-trained models.
- Costs are .30 spread + .10 per side, zero extra latency penalty by default.
  They are explicit quote-price assumptions, not broker measurements. Threefold
  spread/slippage and .50 adverse fill drift are declared stress assumptions.
- Same-bar stop/target ambiguity is stop-first, 1m/2m delay uses future eligible
  opens, missing continuity cannot fabricate a fill, and costs apply once.
- Calibration uses recorded pre-entry probabilities and preserves gross/net
  targets. Heuristic scores never become probabilities. Wilson/block-bootstrap
  intervals and low-sample/null estimates avoid unsupported precision.
- Seeded circular blocks preserve local outcome order within blocks. Resampled
  sequences are not re-sorted into original chronological order. This is sequence
  risk analysis, not guaranteed future probabilities.

## Reports and evidence

PHASE3D_SCORECARD.md records the benchmark, ablation, robustness, calibration and
storage results. The small synthetic validation fixture has **zero eligible closed
outcomes**, not a 0% win rate. Therefore all economic comparisons remain
**INSUFFICIENT_EVIDENCE**, including whether ~75% survives costs or holdout testing.
No strategy or intelligence component is declared superior.

Historical-only research cannot output ELIGIBLE_FOR_HUMAN_REVIEW. Qualifying
validation may report RESEARCH_VALIDATION_PASSED; qualifying holdout still requires
FORWARD_DEMO_REQUIRED. The original Phase 3C promotion gate remains untouched.

Storage profile: 60 one-minute synthetic snapshots contain 3,618,422 canonical
bytes. Linear snapshot-only estimates are 82.82 MiB/1,440 ticks raw, 9.33 MiB with
individual compression and 20.08 MiB for the deduplicated prototype. Fine-grained
deduplication is worse than simple compression on this sample, and physical
small-file overhead is excluded. Exact reconstruction tests pass. The original
~76–78 MiB issue remains a **pre-deployment optimization item**, with no operational
pruning, migration or history deletion implemented.

## Remaining blockers and honest limits

No known failing test or unresolved implementation blocker remains for the offline
research scope. **Evidence is blocked by missing real point-in-time data**, not
by a demonstrated edge. Required: XAU/USD, DXY/USD, 2Y/10Y yields, calendar release/
revision history, financial/geopolitical news editions and receipts, optional Fed,
COT, ETF, physical-market and options/crowding archives, with source attestations.
Keys/entitlements depend on selected providers; the offline lab needs no new key.

Before justified forward DEMO validation: governed experiments, adequate closed
outcomes across regimes/sessions/periods, clean integrity, untouched holdout,
cost/latency sensitivity, calibrated confidence and acceptable risk distributions.
Real-data collection and forward testing require separate authorization/work.
Current in-memory/serial analysis and prototype archival need scale, backup,
access-control and restore testing before operational exposure. No historical
result authorizes live trading.

## Exact files added

1. `backend/research/__init__.py`
2. `backend/research/__main__.py`
3. `backend/research/data.py`
4. `backend/research/archive.py`
5. `backend/research/models.py`
6. `backend/research/execution.py`
7. `backend/research/statistics.py`
8. `backend/research/lab.py`
9. `backend/tests/test_phase3d.py`
10. `backend/tests/test_phase3d_forensic.py`
11. `backend/PHASE3D.md`
12. `backend/PHASE3D_SCORECARD.md`
13. `backend/PHASE3D_REVIEW.md`

No existing tracked source, requirements or workflow file changed. Nothing staged,
committed, pushed or deployed. Android and both ZIPs are unchanged. The pre-existing
untracked Android directory is outside this work. Common secret/private-key/token
patterns were scanned in the new files, with no credential candidates found.
No provider key was read or configured. No broker, notification, UI or Phase 3E
work was added.
