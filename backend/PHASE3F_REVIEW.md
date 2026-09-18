# Phase 3F final forensic review

## Result

**Implementation and offline review complete. Operational readiness: NOT_READY.** All four provider candidates remain unapproved/unconfigured; no production credentials were supplied. Collection and Forex Factory are disabled. Real Forward DEMO cannot safely start yet.

- Phase 3F suite: **142 passed, 0 failed**.
- Complete backend suite: **770 passed, 0 failed** (628 existing tests plus 142 Phase 3F tests).
- One existing dependency deprecation warning from Starlette/AnyIO; no test failures.
- Final 60-tick storage sample: all 12 Champion and 12 Challenger decisions replayed exactly after reopening; storage/hash-chain checks passed.
- No tracked Phase 3E/base files were modified. No staging, commit, deployment, Android changes, broker trading or Phase 4A work occurred.

Commands used:

```text
python -B -m pytest backend/tests/test_phase3f.py backend/tests/test_phase3f_forexfactory.py backend/tests/test_phase3f_forensic.py backend/tests/test_phase3f_final_review.py -q -p no:cacheprovider --tb=short
python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short
```

## Issues found and fixed

1. **Stale relevant-news masking:** fresh unrelated headlines could advance the relevant feed clock. Freshness now uses relevant source publication times only.
2. **Cadence across outages:** calendar/news verification could bridge an arbitrarily long gap. Qualification now requires bounded consecutive receipt spacing.
3. **Truthy authentication flags:** string/integer flags could satisfy boolean gates. Authentication, real transport, entitlement and coverage require actual boolean `True`.
4. **XAU mapping/quote validation:** added explicit unit and response-timezone checks and bid/ask freshness validation; stale quotes cannot appear current.
5. **Higher-frame failures freezing monitoring:** malformed higher-frame values are isolated. Verified valid minute data remains available for exits while missing frames veto entries. Old minute gaps remain entry vetoes and are evaluated from the trade checkpoint for exits.
6. **Wrong calibration scale/type:** reporting now uses the engine's empirical calibration kind and its existing 0–1 probability, without dividing by 100. Challenger calibration and shadow variants are reported separately.
7. **Idle-time qualification:** simply waiting after a tiny trade sample no longer meets the elapsed-period threshold; the observed trade span is used.
8. **Earlier fixture evidence counted as real:** a later valid monitoring tick can no longer qualify a decision whose original capture lacked validated real provenance.
9. **Readiness trusting only the inherited feed label:** the native layer additionally requires source validation evidence on the stored capture; inherited labels alone cannot establish DATA_READY. Continuous collection, restart, replay and momentum-history checks gate FORWARD_DEMO_READY.
10. **Synthetic marker handling:** nested demo/test/sandbox markers are detected, and mocked HTTP cannot establish LIVE. Prior fixture values cannot enter the native momentum-history merge.
11. **Forming-candle excursion leakage:** MFE/MAE now ignore candles that had not closed at the snapshot's receipt-time frontier. Exit-minute extremes are bounds, not invented tick precision.
12. **Outcome analytics rolling back the Champion:** native analytics execute in a savepoint. Failure persists a readiness blocker while retaining the Champion transaction.
13. **Edition storage growth:** repeated identical revisions no longer create unused edition payloads; first-actual preservation no longer rereads the entire edition history.
14. **Conflicting calendar IDs:** inconsistent values under the same event ID within one native response are rejected rather than silently accepted.
15. **Cross-source event identity:** ADP is distinguished from NFP; rate decisions, core/headline releases, monthly/yearly measures and speaker identities are matched conservatively. Both source versions are retained in conflicts.
16. **Merged yield publication frontier:** the combined envelope includes the independently validated publication timestamps of both yields. It cannot import future receipts or future-published records.

## Attack coverage

Tests exercise unapproved providers, missing/expired credentials and entitlements, success without authenticity, HTTP authentication/rate-limit/server/redirect/timeout failures, malformed payloads, nested TEST/FIXTURE markers, daily/delayed values, wrong symbols, nonfinite values, units, UTC offsets, future timestamps, stale/repeated prices, abnormal jumps, market closure, out-of-order observations, duplicate cycles and explicit failover/conflicts.

Calendar/news tests cover as-received editions, future actuals/revisions, earliest observed actual preservation, immutable historical editions, stale relevant news, and duplicate/syndicated handling through the unchanged intelligence tests. Momentum history excludes future receipts and fixture values. Outcome tests prove exactly-once finalization and candle-time boundaries.

Forex Factory tests cover disabled/unvalidated behavior, official JSON requests without scraping, UTC conversion, ambiguous timestamps, duplicate matching, event-family aliases, stale exports, missing source-update timestamps, revisions, conflicts and primary/secondary isolation. A TEST export cannot impose a production veto; a fresh validated secondary conflict is archived with both versions and can veto entry. Forex Factory alone never establishes LIVE_DATA or replaces the primary calendar.

Replay tests cover same-process replay, reopened databases, cold backup/restore, source hashes, compressed snapshots, shared provider references, immutable evidence and companion failure isolation. Synthetic state-machine tests exercise readiness transitions separately from authenticity tests; they are explicitly not real provider certification.

## Security

The intended additions were checked for hardcoded credential assignments, private-key blocks, common access-token patterns and credentials embedded in URLs; no findings. Test credential sentinels are generated dynamically. Tests confirm header-only authentication, sanitized errors, no secret echo persistence, and rejection before acquisition when credentials/approval are absent. No production credential environment values were read or configured during this work.

Native transport uses fixed hosts, no redirects, no ambient HTTP proxy configuration, bounded bodies and request deadlines. Provider exceptions are not logged verbatim. Raw provider bodies are not archived; normalized whitelisted data and hashes are retained. The CLI prints a sanitized operation error. The shipped configuration contains no key values.

These checks are not a guarantee against a malicious provider or privileged database administrator. LIVE classification rests on explicit operator approval plus measured technical checks; only actual entitled-provider validation can establish operational authenticity. Database checks provide tamper evidence, not cryptographic protection from an administrator able to replace the entire database.

## Remaining operational blockers and limitations

- Approve vendors/channels and supply environment-only credentials with the appropriate subscriptions.
- Independently validate the proposed spot XAU, DXY and Treasury instrument mappings, units, timezone semantics and actual update cadence.
- Establish trustworthy calendar coverage, freshness and original-release/revision semantics. The first value observed after a late start is not asserted to be the original release.
- Validate news coverage/latency and revision behavior; receipt time never substitutes for stale source publication time.
- Forex Factory usage, polling permission, export schema and source-update semantics remain unvalidated; it stays optional, disabled and secondary-only.
- Validate real-provider outages, restart recovery and sustained collection using genuine entitled responses. Offline tests do not replace this evidence.
- Provision and optimize storage before deployment; the stress estimate is about 192.84 MiB/day, excluding WAL, backups and mature outcomes. See `PHASE3F_STORAGE.md`.
- Collect sufficient independent real Forward DEMO outcomes across sessions, regimes and time before evaluating performance. No predictive edge, profitability or 75% win rate has been established.

There are no known failing tests or unresolved implementation defects from this review. The above operational/provider limitations prevent a safe real Forward DEMO start.

## Exact intended additions

All paths are repository-relative. No existing tracked source/test/workflow/requirements file was changed.

```text
backend/PHASE3F.md
backend/PHASE3F_REVIEW.md
backend/PHASE3F_STORAGE.md
backend/realdata/__init__.py
backend/realdata/__main__.py
backend/realdata/adapters.py
backend/realdata/collection.py
backend/realdata/config.py
backend/realdata/disabled-config.json
backend/realdata/forexfactory.py
backend/realdata/outcomes.py
backend/realdata/reports.py
backend/realdata/runner.py
backend/realdata/store.py
backend/realdata/transport.py
backend/realdata/verification.py
backend/tests/test_phase3f.py
backend/tests/test_phase3f_final_review.py
backend/tests/test_phase3f_forensic.py
backend/tests/test_phase3f_forexfactory.py
```

The pre-existing untracked `XAU_AI_Trader_Android/` directory is unrelated and was left untouched. Databases, backups, caches and local capacity artifacts are not among these additions.
