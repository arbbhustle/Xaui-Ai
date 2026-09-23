"""Offline recovery preserves originals and never fetches new observations."""
from datetime import timedelta
from contextlib import closing
import sqlite3

import pytest

from backend.domain import digest, stamp
from backend.forward.contracts import select
from backend.mobile.collection import MobileCollector
from backend.mobile.recover import recover
from backend.mobile.runtime import Runtime
from backend.mobile.xau_alignment_v1 import VERSION
from backend.realdata.transport import NativeHTTP
from test_phase1 import NOW


def pending_database(path):
    with Runtime(path) as runtime:
        selection = select((), [], NOW)
        capture = {'observed_at': stamp(NOW), 'started_at': stamp(NOW),
                   'observations': [], 'provider_audit': [], 'selection': selection,
                   'vetoes': selection['vetoes']}
        cycle = digest(['offline-recovery-test', stamp(NOW)])
        with runtime.store.transaction() as conn:
            capture_id = runtime.store.put_capture(conn, capture)
            conn.execute('INSERT INTO forward_cycles VALUES (?,?,?,?,?)',
                         (cycle, stamp(NOW), 'CAPTURED', capture_id, stamp(NOW)))
            runtime.store.event(conn, 'capture:' + cycle, 'CAPTURE', stamp(NOW), {'capture_id': capture_id})


def pending_count(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT count(*) FROM forward_cycles WHERE status='CAPTURED'").fetchone()[0]


def test_review_then_apply_preserves_backup_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(NativeHTTP, 'read', lambda *a, **k: pytest.fail('Recovery fetched provider data'))
    monkeypatch.setattr(MobileCollector, 'start', lambda *a: pytest.fail('Recovery started scheduler'))
    path = tmp_path / 'mobile.sqlite3'
    pending_database(path)
    # CAPTURED is durable recovery state and normal startup may recover it.
    # Explicit offline recovery remains available for stopped-service review.
    assert recover(path)['status'] == 'RECOVERY_REVIEW_PASSED'
    assert pending_count(path) == 1
    assert not list(tmp_path.glob('*.pre-recovery-*.sqlite3'))
    assert recover(path, apply=True)['status'] == 'RECOVERY_APPLIED'
    archives = list(tmp_path.glob('*.pre-recovery-*.sqlite3'))
    assert len(archives) == 1 and pending_count(archives[0]) == 1
    with Runtime(path) as runtime:
        assert runtime.replay['status'] == 'NO_DECISIONS_YET'
        assert pending_count(path) == 0
    assert recover(path, apply=True)['status'] == 'NO_PENDING_CYCLES'
    assert len(list(tmp_path.glob('*.pre-recovery-*.sqlite3'))) == 1


def test_failed_review_never_changes_source(tmp_path, monkeypatch):
    path = tmp_path / 'mobile.sqlite3'
    pending_database(path)
    original = path.read_bytes()
    def fail(self):
        raise ValueError('review failure')
    original_recover = MobileCollector._recover
    calls = {"count": 0}
    def fail_once(self):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError('review failure')
        return original_recover(self)
    monkeypatch.setattr(MobileCollector, '_recover', fail_once)
    with pytest.raises(ValueError, match='review failure'):
        recover(path, apply=True)
    assert path.read_bytes() == original
    assert pending_count(path) == 1
    assert not list(tmp_path.glob('*.pre-recovery-*'))


def test_identity_mismatch_never_changes_source(tmp_path, monkeypatch):
    path = tmp_path / 'mobile.sqlite3'
    pending_database(path)
    original = path.read_bytes()
    monkeypatch.setenv('MOBILE_XAU_VALIDATOR', VERSION)
    with pytest.raises(RuntimeError, match='ORIGINAL_ENGINE_CONFIGURATION_REQUIRED'):
        recover(path, apply=True)
    assert path.read_bytes() == original


def test_refuses_running_writer(tmp_path):
    path = tmp_path / 'mobile.sqlite3'
    with Runtime(path):
        with pytest.raises(RuntimeError, match='Only one monitor'):
            recover(path, apply=True)


def test_interrupted_collector_replays_existing_decisions(tmp_path, monkeypatch):
    from test_mobile_collection import approve, wire
    approve(monkeypatch)
    path = tmp_path / 'mobile.sqlite3'
    clock = [NOW]
    with Runtime(path) as runtime:
        wire(runtime, monkeypatch, clock)
        for minute in range(3):
            clock[0] = NOW + timedelta(minutes=minute)
            assert runtime.collector._once().get('status') != 'COLLECTION_OR_INTEGRITY_FAILURE'
        with runtime.read() as conn:
            prior = [tuple(row) for row in conn.execute('SELECT * FROM decisions')]
        assert prior
        clock[0] += timedelta(minutes=2)
        def crash(*args, **kwargs):
            raise RuntimeError('interrupted after capture')
        monkeypatch.setattr(runtime.engine, 'tick', crash)
        assert runtime.collector._once()['status'] == 'COLLECTION_OR_INTEGRITY_FAILURE'
    monkeypatch.setattr(NativeHTTP, 'read', lambda *a, **k: pytest.fail('Recovery fetched data'))
    result = recover(path, apply=True)
    assert result['checked_decisions'] >= len(prior)
    with Runtime(path) as runtime:
        assert runtime.replay['status'] == 'PASSED'
        with runtime.read() as conn:
            after = [tuple(row) for row in conn.execute('SELECT * FROM decisions')]
        assert all(row in after for row in prior)
