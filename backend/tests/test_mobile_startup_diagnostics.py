"""Startup diagnostics must remain useful without disclosing arbitrary exceptions."""
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import traceback

import pytest
from fastapi.testclient import TestClient

from backend.mobile.api import STARTUP_CODES, create_app, startup_diagnostic


@pytest.mark.parametrize('code', sorted(STARTUP_CODES))
def test_known_codes(code):
    assert startup_diagnostic(RuntimeError(code)) == f'type=RuntimeError code={code}'


@pytest.mark.parametrize('error', [
    RuntimeError('API_KEY_SECRET'),
    RuntimeError('ORIGINAL_ENGINE_CONFIGURATION_REQUIRED /var/data/private.sqlite3'),
    ValueError('token=private-secret'),
    PermissionError(13, 'secret', '/var/data/private.sqlite3'),
    sqlite3.OperationalError('private-secret /var/data/private.sqlite3'),
    type('SECRET_CLASS_NAME', (Exception,), {})('private-secret'),
    RuntimeError(['private-secret']),
])
def test_startup_traceback_redacts_unknown_content(error):
    def broken(_):
        raise error

    with pytest.raises(RuntimeError) as caught:
        with TestClient(create_app(runtime_factory=broken)):
            pytest.fail('Startup must fail closed')
    rendered = ''.join(traceback.format_exception(caught.value))
    assert 'phase=MAIN_RUNTIME' in str(caught.value)
    assert 'code=UNCLASSIFIED' in str(caught.value)
    for private in ('API_KEY_SECRET', '/var/data/', 'private-secret', 'SECRET_CLASS_NAME'):
        assert private not in rendered
    assert caught.value.__suppress_context__


def test_research_failure_reports_phase_and_releases_main_runtime():
    closed = []

    @contextmanager
    def runtime(_):
        try:
            yield SimpleNamespace(path='unused', collection_enabled=False)
        finally:
            closed.append(True)

    def research(**kwargs):
        raise RuntimeError('INVALID_RESEARCH_US2Y_FLAG')

    with pytest.raises(RuntimeError, match='phase=RESEARCH_RUNTIME type=RuntimeError code=INVALID_RESEARCH_US2Y_FLAG'):
        with TestClient(create_app(runtime_factory=runtime, research_runtime_factory=research)):
            pytest.fail('Startup must fail closed')
    assert closed == [True]


def test_worker_lock_message_is_mapped_to_fixed_code():
    assert startup_diagnostic(RuntimeError('Only one monitor process may use this database')) == (
        'type=RuntimeError code=DATABASE_WORKER_LOCKED'
    )
