# DardaniaXAUTRADE AI — Phase 1

The parallel, local-only Phase 2 council is documented in [PHASE2.md](PHASE2.md).
The separate Phase 3A macro/news layer is documented in [PHASE3A.md](PHASE3A.md).
`backend.main:app` remains the Phase 1 entry point; `backend.phase2_main:app` uses
a separate demo database. No deployment or Android URL change is included.

Demo-only reliability backend. No broker APIs, broker credentials, orders, news,
macro council, push notifications, calibration, or Android UI changes are included.

## Source and scope

The current engine was found in
[`arbbhustle/xau-ai-trader-android`](https://github.com/arbbhustle/xau-ai-trader-android/tree/6280c300b23920c8bb8d0ac2301f34dfdcad1b73),
commit `6280c300b23920c8bb8d0ac2301f34dfdcad1b73`.
Its adaptive scoring helpers and score/ATR trade-plan calculations were carried
into `scoring.py`. The clock is now an explicit argument. The scoring math is
unchanged; closed-bar selection, confirmation, execution, and risk controls are new.

This is a local implementation in the Android repository's `backend/` directory.
It does not modify that remote repository or deploy to Render. The Android URL
remains `https://xau-ai-trader-android.onrender.com/signal`.

## Behavior

- The worker runs immediately on startup and at UTC minute boundaries plus ten
  seconds. Its lifetime is independent of HTTP requests and the Android app.
- Twelve Data supplies UTC `1min`, `5min`, `15min`, `1h`, and `4h` candles.
  The 1m request fetches up to 1,000 rows for recovery; other intervals fetch 125.
  Higher intervals cache only until the next expected close. Provider errors are
  not replaced by synthetic data or silently stale cached data.
- Only completed candles are used. One immutable decision is journaled per
  completed 5m candle. Incomplete bars never alter the 5m score.
- 15m buy-minus-sell score must agree with the candidate (minimum absolute edge 8).
  An opposing 1H or 4H edge of at least 14 vetoes the candidate. These use the
  original score formula as a directional measurement, not its BUY/SELL threshold.
- A persisted signal-episode flag prevents repeated entries during the same
  consecutive BUY or SELL episode, including after restart, closure, or cancellation.
  A valid new opposite or NO_TRADE 5m candidate resets the episode; outages do not.
- GET endpoints only read. They cannot fetch market data, generate decisions,
  advance trades, reset history, or mutate the engine.

## Persistent storage

SQLite uses WAL, FULL synchronous writes, foreign keys, transactional decisions,
and unique constraints for the 5m cycle, originating trade, and active position.
Only one pending/open trade is allowed. A process lock prevents two monitor
processes from sharing this database. Run one service instance and one Uvicorn worker.

Tables: `schema_version`, `snapshots`, `decisions`, `trades`, `trade_events`, `state`.
Snapshots contain the input candles, observation time, policy, and source-code hash.
Decisions contain the historical execution context. The CLI checks the snapshot
hash and recomputes scores and execution gates using that context.
Trade events record the exact monitoring candle and resulting state. A cursor
prevents reprocessing a candle after restart. All state changes for a tick commit
together or roll back together.

SQLite is deliberately limited to a durable local disk on one host for this
phase; it is not a multi-replica database. PostgreSQL remains a future migration.
No retention job deletes demo history. Monitor disk growth and archive snapshots
with a tested policy before a long-running deployment.

## Entry and exit model

1. An accepted 5m decision creates a PENDING demo trade, never a broker order.
2. Only a full 1m candle starting at or after the next minute boundary can fill it.
   This prevents pre-decision highs/lows from closing a newly created trade.
3. The next eligible candle's open is the simulated fill, observed once that
   candle closes. A fill farther than 0.5 × 5m ATR from the plan is cancelled.
4. SL/TP distances are retained and rebased to the simulated fill. The pending
   plan expires at the next 5m close. A risk veto can cancel a pending plan only
   before its eligible opening time. After that time, resolve historical candles
   first: a later veto or expiry cannot erase an earlier fill or SL/TP event.
   A forming or missing fill candle leaves the plan unresolved until recovered.
5. OPEN trades process all unprocessed eligible 1m bars chronologically. TP1 is
   marked but does not partially close the trade (same policy as the original).
   TP2 closes it. If SL and a target touch in the same candle, SL wins and an
   ambiguity flag is stored. Gaps through SL use the worse bar-open price.
6. R is measured against the entry's initial stop distance. These are **gross
   candle-based demo results**: no bid/ask execution, commissions, financing, or
   general slippage model is claimed.

There is no retrospective new trade for every missed 5m cycle: restart catches up
existing trades and evaluates the latest cycle only. If required 1m bars are absent,
the existing position remains unresolved with `MONITOR_CATCHUP_GAP`; new entries
are blocked. Do not manually mark the trade a winner or loser. Restore the missing
data in a reviewed recovery procedure before resuming. No public mutation/reset
endpoint is exposed.

## Risk vetoes and data health

- Missing, malformed, duplicate, overlapping, future, or unexpectedly gapped data.
  Each feed is independently validated before hashing; non-finite/malformed
  values are stored as safe JSON rejection inputs for reproducible decisions.
  Entry validation uses the full lookback. Position monitoring validates only
  bars from its next unprocessed checkpoint (or first eligible entry) onward,
  without a 60-bar minimum. Higher-timeframe failures cannot block valid 1m exits.
- Fewer than 60 completed candles in any required interval.
- Latest candle age at least its timeframe plus 90 seconds.
- Worker heartbeat older than 150 seconds, or clock going backwards.
- Closed gold session (New York: daily 17:00–18:00 break and Friday 17:00 through
  Sunday 18:00). Expected session gaps are allowed. Holidays/early closures are
  conservatively handled by stale-data vetoes; a full holiday calendar is not included.
- 15m confirmation failure or opposing 1H/4H bias.
- Original volatility detector returns HIGH; invalid ATR or trade-plan geometry.
- 1m close is more than 0.5 × 5m ATR from the 5m entry.
- Existing active position, duplicate episode, or expired signal.
- Sum of realized losing trades reaches 3R in the UTC day (winning trades do not
  replenish this allowance).
- `DEMO_KILL_SWITCH=true` prevents new entries; existing trades still get monitored.

Policy defaults are versioned in `domain.py`. These thresholds are starting
operational safeguards, not validated trading optimizations. CPI/NFP/FOMC,
spread-aware vetoes, and external price comparisons are not implemented in this phase.

## API compatibility

`GET /signal` retains the Android fields: direction, confidence, buy/sell scores,
entry, SL/TPs, RSI, ATR, reasons, session, mode, source, demo trade and performance.
It adds candle timestamps, expiry, timeframe bias, risk codes, decision/snapshot
IDs and health. Stale/failed/expired reads return NO_TRADE and clear SL/TP values.
`timestamp_utc` remains decision time; `served_at` is response time.
Source candle close timestamps are checked again at response time, independently
of the worker heartbeat. Signal reads check both the decision's source data and
current worker health. History responses label archived decisions HISTORICAL or
STALE and retain their original label in `observed_data_status`; stored records
are never rewritten. Performance and trade responses include current data health.

`confidence` retains the original normalized score for Android compatibility;
`confidence_kind=UNCALIBRATED_SCORE` explicitly describes it. It is not a learned
probability. The unchanged Android UI still labels this value Confidence.

Other read routes: `/performance`, `/trades` (cursor pagination), `/history`,
`/strategy`, `/market-status`, `/health`, `/live`.
The old public `/reset-demo` and `/analyze` mutation routes are intentionally absent.

`/health` returns 503 for unhealthy/stale data; `/live` is process liveness. Use
`/live` as the hosting health-check path so market closure does not restart the app.

## Local run

Run from the repository root with Python 3.12:

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/requirements-dev.txt
$env:TWELVE_DATA_API_KEY = '<your key>'
$env:DEMO_DB_PATH = 'backend/data/demo.sqlite3'
backend/.venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Missing credentials produce an unhealthy, non-trading service; never demo prices
labelled LIVE_DATA. There is no key in the repository. The server does not log raw
provider errors or URLs, which can contain credentials.

Requests are bounded with HTTP timeouts and run concurrently across timeframes.
The provider plan must support 1m XAU/USD history and the polling quota. Minute
monitoring alone uses roughly one request/minute, plus requests at higher-timeframe
boundaries and retries. Verify plan limits before deployment.

## Tests

```powershell
backend/.venv/Scripts/python -m pytest backend/tests -q
```

Tests use deterministic synthetic fixtures and isolated temporary databases, not
the production service. They cover scores, candle completion, timeframe vetoes,
bad data, concurrency, restart/recovery, pre-entry exclusion, stop/target handling,
duplicate episodes, daily loss veto, read-only API, stale heartbeats, backup/restore,
snapshot replay/tampering, worker locking, feed errors, and secret-safe logging.

## Backups and replay

Use SQLite's online backup, not a copy of the database file without its WAL:

```text
python -m backend.manage --database backend/data/demo.sqlite3 backup backup.sqlite3
python -m backend.manage --database backend/data/demo.sqlite3 replay DECISION_ID
```

Replay requires the matching source-code version. Retain release commits alongside
backups. Version `phase1-2` hashes named UTF-8 source files with normalized line
endings, so Windows CRLF and Linux LF checkouts share an identity. Earlier code
hashes still require their original release; this patch does not rewrite history.
Restore by stopping the service, retaining the current database for rollback,
and pointing `DEMO_DB_PATH` at a verified restored backup. Never overwrite an active
database. The CLI refuses to overwrite an existing backup destination.

## Render deployment prerequisites — not deployed by this change

The existing service is configured as free in its source. Render's default
filesystem is ephemeral; a file there is not persistent storage. This phase needs
an always-running instance and an attached persistent disk. No paid resources have
been created and no deployment settings have been changed.

For a reviewed deployment of this repository:

- Build: `pip install -r backend/requirements.txt`
- Start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1`
- Disk mount: `/var/data`
- `DEMO_DISK_PATH=/var/data`
- `DEMO_DB_PATH=/var/data/demo.sqlite3`
- `TWELVE_DATA_API_KEY`: secret in Render, not in Git.
- Health-check path: `/live`
- One instance; no multi-worker Uvicorn configuration.

When `RENDER` is set, startup refuses a database outside an actual mounted disk.
Keep the existing service/hostname to retain the Android URL. Deploying from the
separate backend repository instead requires copying this `backend/` package there
and using the same commands.

Old in-memory history cannot be reconstructed from aggregates. This database starts
a clearly separate history; importing old individual trades requires a reviewed
export/import migration. Do not claim previous win rates are carried forward.

Before activation: verify persistent disk mount, backup/restore, actual feed cadence,
UTC candle alignment, account quota, and stop/restart recovery in a demo staging
instance. Do not enable live broker trading.

Reference: https://render.com/docs/disks
