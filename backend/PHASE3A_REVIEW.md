# Phase 3A final forensic review

Reviewed locally on 2026-09-16 against committed Phase 2 base
`43d4b1d263c382a4dcbafe44a12caad8b7aad51a`.
No staging, commit, deployment, Android edits, broker trading or Phase 3B work.

## Result

Complete backend suite: **261 passed**, including **32 new forensic cases** in
`tests/test_phase3a_forensic.py`. One dependency deprecation warning remains:
Starlette's TestClient references AnyIO's deprecated BlockingPortal alias.
Tests are offline, use deterministic synthetic fixtures, and use temporary databases.

Command: `python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short`.
The initial forensic reproductions produced 18 failures and one passing control;
all now pass. Additional regressions cover the subsequently identified risks.

## Issues found and fixed

| Finding | Correction and regression evidence |
|---|---|
| Higher-timeframe candles accepted minute-aligned but timeframe-misaligned timestamps | Validate UTC epoch boundaries for each interval; tests for 5m, 15m, 1H and 4H. |
| Malformed forming 1m candle froze known completed-bar exits | Exclude forming OHLC before validating prices; regression closes an already-open stop loss despite a malformed forming bar. Future opening timestamps still fail validation. |
| Credential URL aliases escaped rejection | Reject auth, authorization, sig and credential aliases as well as existing token/key forms; five regression cases. |
| Envelope labels erased record/adapter fixture or delayed provenance | Preserve restrictive record provenance, require registered successful adapter modes, and prevent adapter FIXTURE/DELAYED promotion. |
| Partial unavailable intelligence aggregated as LIVE | Required freshness/availability now prevents aggregate LIVE labeling. Individual source provenance remains separately visible. |
| Strict API served a previously opted-in fixture BUY | Response guards enforce current fixture policy without modifying stored historical decisions. |
| Replay could miss execution-context tampering when output stayed unchanged | Seal the full decision, including execution context and monitor metadata; reject checksum mismatch before replay. |
| Equal-time news revisions selected an arbitrary interpretation | Incompatible same-publisher/id/time editions produce CONFLICTING_NEWS_REVISION. |
| Syndication refreshed conflict weight despite decayed story scoring | Both scoring and high-impact conflict detection use the group's earliest known publication. |
| Original publication disappeared between polls, rejuvenating a story | Persist hashed identities and earliest publication transactionally; restart/replay regression covers a new publisher/id/URL with the same headline. |
| Ancient calendar disagreements vetoed current decisions | Ignore obsolete groups/revisions outside the past risk horizon; retain relevant and upcoming revision conflict checks. |
| Calibration identity used a set of modes, losing channel assignments | Hash the channel-to-mode mapping, including XAU; swapped channel modes now produce distinct model identities. |
| XAU had no separate provenance; unrelated fixture/real prices could affect trade state | Capture gold provider/mode, veto unknown/future receipt metadata, bind database input category, and block incompatible prices from advancing a position checkpoint. Synthetic gold cannot be hidden by LIVE intelligence. |
| Pairwise source-conflict detection scaled quadratically | Replace with positive/negative source sets; a 1,000-record regression checks bounded linear reads. |

## Review coverage

1. **Look-ahead:** observation/publication/source/receipt ordering fails closed;
   only closed, aligned XAU candles are eligible. Calibration retains the Phase 2
   historical cutoff. News identity memory excludes future-known envelopes/records.
2. **Timestamps:** normalize aware timestamps to UTC; equivalent-offset audit
   regression passes. Future event schedules remain distinct from future publication.
3. **Freshness:** per-channel TTL and separate quote-observation age checks;
   API recalculates current health, including the original decision snapshot.
4. **Unavailable/LIVE:** defaults return unavailable; missing critical feeds veto;
   incomplete fresh-source sets cannot aggregate as LIVE.
5. **Fixtures:** restrictive provenance, runtime permission, gold provenance,
   calibration separation and database/position isolation are tested.
6. **Duplicates:** ID/URL/title news grouping plus persistent earliest-publication
   identity; calendar latest revisions grouped by stable event occurrence key.
7. **Conflicts:** opposing high-impact sources and incompatible revisions veto;
   dedup does not hide opposing reports or restore old reports' impact.
8. **Blackouts:** inclusive pre/post boundary tests for CPI 60/30 minutes,
   NFP 45/30, PCE 30/20 and FOMC 90/60; explicit duration extends the post window.
9. **Council:** technical scores remain separate; original timeframe vetoes remain.
   Opposing intelligence cannot reverse the strong BUY/SELL fixture cases. With
   all components, maximum intelligence contribution to the directional edge is
   30 points versus 70% of the technical edge; weaker evidence can legitimately
   change candidate/confidence. These weights remain unvalidated model assumptions.
10. **Replay:** snapshot and full decision checksums, captured journal context,
    original policies/outcomes, deterministic ordering, portable source hashes.
11. **Failure:** per-channel exception isolation, bounded acquisition and one
    outstanding call per channel; macro outages do not block valid same-mode exits.
12. **Security:** 47 source/document/config text files and 26 text entries across
    both tracked ZIPs scanned for credential patterns with no candidates.
    Existing failure tests verify exception redaction.
    No credential values were printed. This is pattern scanning and code review,
    not proof against secrets deliberately disguised as public source text.
13. **Performance/storage:** linear conflict check; acquisition bounded separately;
    history and identity growth remain operational considerations below.

## Measurements and remaining blockers

- Local small fixture tick: approximately **41 ms**. Snapshot payload **55,112
  bytes**, decision **9,590**, trade **7,497**, initial event **7,497**.
  Snapshot-only extrapolation: **75.68 MiB per 1,440 ticks**, excluding database
  overhead, decisions, trade events and the identity journal. Real maximum-size
  provider payloads can be substantially larger. No history was purged.
  **Pre-deployment optimization item:** reduce this approximately 76 MiB per
  1,440-tick growth while preserving complete deterministic replay and trade history.
- A normalized 1,000-headline assessment took approximately **33 ms** locally;
  this is a smoke measurement, not a production concurrency benchmark.
- Before continuous real-provider DEMO use: select licensed providers, implement
  and validate adapters, supply credentials securely, verify actual timestamp and
  calendar-coverage semantics, and plan durable storage, backup/restore, disk limits
  and retention/archive rules that preserve replay. No real provider integration
  or latency/failure behavior has been validated against a network service.
- Heuristic sentiment, exact-headline identity and gold-impact mappings need
  historical/forward-demo validation. Identical headlines can conservatively merge
  distinct recurring stories; differently worded syndication can escape dedup.
- Trusted adapters must honestly declare provenance and supply point-in-time data.
  The contract cannot discover fabrication or historical revisions that a source
  falsely labels as original. Checksums are integrity checks, not signatures.
- Uncommitted older Phase 3A databases do not receive fabricated checksum or
  provenance backfills. Keep original code for historical replay; use a separate
  fresh database for this candidate. Preserve any old database as an archive.
- No known unresolved blocker to committing this local, fail-closed DEMO candidate.
  The requirements above block operational activation, not offline tests.

## Files changed during this forensic review

- `backend/domain.py`
- `backend/engine.py`
- `backend/feed.py`
- `backend/intelligence.py`
- `backend/intelligence_providers.py`
- `backend/intelligence_journal.py` (new)
- `backend/phase3a.py`
- `backend/tests/intelligence_fixtures.py`
- `backend/tests/test_phase3a.py`
- `backend/tests/test_phase3a_forensic.py` (new)
- `backend/PHASE3A.md`
- `backend/PHASE3A_REVIEW.md` (new)

Earlier unstaged Phase 3A work also includes `.github/workflows/backend-tests.yml`,
`backend/README.md`, `backend/council.py`, `backend/main.py`, `backend/manage.py`,
`backend/phase2.py` and `backend/phase3a_main.py`.

Android: 18 extracted source files match the original AURUM CORE v2 ZIP byte for
byte. Both ZIPs remain unchanged. The pre-existing untracked Android directory
remains untracked. Render was not contacted or deployed. Requirements unchanged.
