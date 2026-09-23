"""Explicit offline recovery: rehearse on a copy, audit, then optionally apply.

Never called by API startup. Stop the service writer before invoking this module.
Only stored captures are used; no collector thread or provider request is started.
"""
import argparse
from contextlib import closing, contextmanager
from pathlib import Path
import json
import gc
import os
import shutil
import sqlite3
import tempfile
import uuid

from .runtime import Runtime
from .collection import MobileCollector


@contextmanager
def review_workspace(parent):
    with tempfile.TemporaryDirectory(prefix='mobile-recovery-', dir=parent) as work:
        try:
            yield work
        finally:
            # Frozen RealStore constructors leave read-only SQLite handles for GC.
            # Release them before removing trial files, including on Windows.
            gc.collect()


def backup(source, destination):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def recover(database=None, apply=False):
    owner = Runtime(database)
    if not owner.path.is_file():
        raise RuntimeError('RECOVERY_DATABASE_REQUIRED')
    # The same lock as API startup protects the complete review and application.
    with owner.lock:
        with closing(sqlite3.connect(owner.path.as_uri() + '?mode=ro', uri=True)) as conn:
            pending = conn.execute("SELECT count(*) FROM forward_cycles WHERE status!='COMPLETE'").fetchone()[0]
            unsupported = conn.execute("SELECT count(*) FROM forward_cycles WHERE status NOT IN ('CAPTURED','COMPLETE')").fetchone()[0]
        if unsupported:
            raise RuntimeError('RECOVERY_UNKNOWN_CYCLE_STATUS')
        if not pending:
            return {'status': 'NO_PENDING_CYCLES', 'applied': False}
        storage = owner.storage()
        if (not storage['within_budget'] or storage['free_bytes'] <
                3 * storage['allocated_bytes'] + owner.reserve + 16 * 1024 * 1024):
            raise RuntimeError('RECOVERY_HEADROOM_REQUIRED')
        with review_workspace(owner.path.parent) as work:
            original = Path(work) / 'original.sqlite3'
            trial = Path(work) / 'trial.sqlite3'
            backup(owner.path, original)
            backup(original, trial)
            # Runtime startup now accepts CAPTURED cycles so the normal single-owner
            # collector can deterministically recover them. For explicit offline
            # recovery, open the trial copy normally and replay only its archived
            # captures; provider reads and scheduler startup remain disabled.
            with Runtime(trial) as reviewed:
                MobileCollector(reviewed)._recover()
            # Normal startup validates every stored decision and challenger replay.
            with Runtime(trial) as verified:
                checked = verified.replay['checked_decisions']
            result = {'status': 'RECOVERY_REVIEW_PASSED', 'pending_cycles': pending,
                      'checked_decisions': checked, 'applied': False}
            if apply:
                # Publish an exclusive complete original backup before touching source.
                archive = owner.path.with_name(owner.path.name + '.pre-recovery-' + uuid.uuid4().hex + '.sqlite3')
                with archive.open('xb') as target, original.open('rb') as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                # SQLite backup replaces the destination in one SQLite transaction,
                # handling WAL correctly. The original remains in the archive above.
                backup(trial, owner.path)
                result.update(status='RECOVERY_APPLIED', applied=True, backup_preserved=True)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(recover(args.database, args.apply)), flush=True)
    except Exception as exc:
        from .api import startup_diagnostic
        safe_codes = {'RECOVERY_DATABASE_REQUIRED', 'RECOVERY_UNKNOWN_CYCLE_STATUS',
                      'RECOVERY_HEADROOM_REQUIRED', 'RECOVERY_PENDING_STATE_CHANGED',
                      'UNCHECKPOINTED_ENGINE_STATE', 'UNVERIFIED_CAPTURE_ALIGNMENT',
                      'CAPTURE_FRAME_MISMATCH', 'CAPTURE_CLOCK_MISMATCH'}
        code = exc.args[0] if (type(exc) in (RuntimeError, ValueError) and len(exc.args) == 1
                              and type(exc.args[0]) is str and exc.args[0] in safe_codes) else None
        diagnostic = 'code=' + code if code else startup_diagnostic(exc)
        print('MOBILE_OFFLINE_RECOVERY_FAILED ' + diagnostic, flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
