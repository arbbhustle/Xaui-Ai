from datetime import timedelta

from backend.domain import stamp
from backend.mobile.research_runtime import ResearchUS2YRuntime
from test_phase1 import NOW

import pytest

@pytest.fixture(autouse=True)
def enable_research(monkeypatch):
    monkeypatch.setenv(
        "MOBILE_RESEARCH_US2Y_ENABLED",
        "true",
    )


def fake_result(now, value=4.7583):
    observed = now.replace(second=0, microsecond=0) - timedelta(minutes=1)

    return {
        "provider": "twelve-data-us2y",
        "channel": "us2y",
        "symbol": "US2Y",
        "status": "OK",
        "data_mode": "RESEARCH_ONLY",
        "retrieved_at": stamp(now),
        "as_of": stamp(observed),
        "records": [
            {
                "id": f"US2Y:{stamp(observed)}",
                "source": "Twelve Data",
                "url": "https://twelvedata.com",
                "published_at": stamp(observed),
                "observed_at": stamp(observed),
                "instrument": "US2Y",
                "value": value,
            }
        ],
    }


def test_research_runtime_stores_us2y_offline(tmp_path, monkeypatch):
    path = tmp_path / "us2y.sqlite3"

    monkeypatch.setattr(
        "backend.mobile.research_runtime.fetch_us2y",
        lambda now=None: fake_result(now),
    )

    runtime = ResearchUS2YRuntime(path=path, clock=lambda: NOW)

    result = runtime.once()

    assert result["status"] == "COMPLETE"
    assert result["inserted"] == 1

    rows = runtime.snapshot(NOW)

    assert len(rows) == 1
    assert rows[0]["value"] == 4.7583
    assert rows[0]["provider"] == "twelve-data-us2y"


def test_research_runtime_duplicate_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "us2y.sqlite3"

    monkeypatch.setattr(
        "backend.mobile.research_runtime.fetch_us2y",
        lambda now=None: fake_result(now),
    )

    runtime = ResearchUS2YRuntime(path=path, clock=lambda: NOW)

    first = runtime.once()
    second = runtime.once()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert len(runtime.snapshot(NOW)) == 1


def test_research_runtime_status_is_fresh(tmp_path, monkeypatch):
    path = tmp_path / "us2y.sqlite3"

    monkeypatch.setattr(
        "backend.mobile.research_runtime.fetch_us2y",
        lambda now=None: fake_result(now),
    )

    runtime = ResearchUS2YRuntime(path=path, clock=lambda: NOW)

    runtime.once()

    status = runtime.status(NOW)

    assert status["status"] == "HEALTHY"
    assert status["freshness"] == "FRESH"
    assert status["mode"] == "RESEARCH_ONLY"
    assert status["channel"] == "us2y"
    assert status["symbol"] == "US2Y"


def test_research_runtime_becomes_stale(tmp_path, monkeypatch):
    path = tmp_path / "us2y.sqlite3"

    monkeypatch.setattr(
        "backend.mobile.research_runtime.fetch_us2y",
        lambda now=None: fake_result(now),
    )

    runtime = ResearchUS2YRuntime(path=path, clock=lambda: NOW)

    runtime.once()

    later = NOW + timedelta(minutes=20)
    status = runtime.status(later)

    assert status["status"] == "UNAVAILABLE"
    assert status["freshness"] == "STALE"


def test_research_runtime_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "us2y.sqlite3"

    monkeypatch.setattr(
        "backend.mobile.research_runtime.fetch_us2y",
        lambda now=None: fake_result(now),
    )

    first = ResearchUS2YRuntime(path=path, clock=lambda: NOW)
    first.once()

    second = ResearchUS2YRuntime(path=path, clock=lambda: NOW)

    rows = second.snapshot(NOW)

    assert len(rows) == 1
    assert rows[0]["value"] == 4.7583