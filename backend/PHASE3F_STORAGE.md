# Phase 3F storage and replay validation

## Final offline measurement

Measured after the final implementation using **60 consecutive one-minute TEST_DATA ticks**, six native-shaped primary channels, 1,000 closed 1m candles plus 120 candles for each higher timeframe, 30 calendar records and 40 news records per acquisition. The stress fixture changes calendar/news editions each minute. It is generated offline, is not real market evidence, and cannot be selected by the production collector. The test harness supplies explicitly fixture-labelled candles directly to the engine to exercise full snapshot persistence and replay.

The run produced 12 five-minute Champion decisions, 12 Challenger decisions and 120 capture/decision ledger entries. It did not generate eligible real trades. Temporary databases were kept outside the repository and are not deliverables.

| Measure | Result |
|---|---:|
| Actual allocated database, 60 ticks | 8,425,472 bytes / 8.04 MiB |
| Raw snapshot payloads, 60 ticks | 8,118,698 bytes |
| Compressed snapshot payloads, 60 ticks | 640,824 bytes |
| Snapshot payloads per 1,440 ticks | approximately 14.67 MiB |
| Complete database per hour | approximately 8.04 MiB |
| Complete database per 1,440 ticks/day | approximately 192.84 MiB |
| Complete database per 30 days | approximately 5,785.32 MiB / 5.65 GiB |
| Complete database per 365 days | approximately 70,388.06 MiB / 68.74 GiB |

These are linear sample extrapolations, not validated real-production forecasts. They exclude WAL/SHM, backups, filesystem overhead, mature trade/learning history, and optional Forex Factory export volume. Market closure does not imply automatic retention or deletion. Calendar/news revision rates, actual provider coverage and selected history lengths will materially affect growth. SQLite index page allocation may vary slightly with content hashes.

## Comparison with Phase 3E

Phase 3E documented approximately **9.36 MiB snapshot payloads and 79.03 MiB complete database per 1,440 ticks/day**. This larger native-shaped Phase 3F stress scenario projects about **2.44 times** that complete-database volume. It is a different workload and does not establish an apples-to-apples storage regression or an optimization percentage.

Shared provider objects and compressed replay snapshots remain intact. Repeated unchanged news/calendar editions no longer create unreferenced per-receipt edition payloads. First-observed actual lookup uses the latest persisted edition instead of rereading every prior edition.

The larger remaining costs include native edition rows/indexes, immutable provider evidence, snapshots and council journals. **Storage provisioning, retention/archival design and growth optimization remain pre-deployment items.** No evidence pruning, destructive migration or database replacement was performed. Before deployment, profile genuine provider payload rates and active demo outcomes over a substantially longer period, including backup/restore and concurrent-reader load.

## Replay integrity

After reopening the final sample with the same code/configuration:

- 12/12 Champion decisions replayed exactly.
- 12/12 Challenger decisions replayed exactly.
- Compressed snapshot hashes, shared-object hashes and all 120 ledger entries passed integrity verification.
- Automated tests also verify cold SQLite backup/restore, restart behavior, duplicate-cycle suppression, test-data exclusion and Champion/Challenger isolation.

Implementation hashes deliberately change when source changes. Reproduce an older sample with its original implementation/configuration; do not relabel an incompatible replay as passing.

## Reproduction

The offline workload builder is `offline_cycle` in `tests/test_phase3f_forensic.py`. Construct a disabled `RealRunner` with the six test specifications from `tests/test_phase3f.py`, call the builder for 60 consecutive minutes with `large=True`, then call `realdata.reports.capacity`. Reopen the database and replay every ID in `decisions` using both `engine.replay` and `engine.replay_meta`. This harness is test-only and is never imported by service code. Use a temporary database outside the repository.
