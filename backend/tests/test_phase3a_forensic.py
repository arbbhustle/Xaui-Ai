"""Forensic regressions: each failure reproduces a concrete review finding."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient

from backend.domain import closed_frame, parse, stamp
from backend.intelligence import IntelligencePolicy, conflict
from backend.intelligence_providers import IntelligenceFeed, normalize_bundle, source_url, MARKET_KEY
from backend.phase3a import Phase3AEngine
from backend.phase3a_main import create_phase3a_app
from backend.storage import Store
from test_phase1 import NOW, frame, active, append_minute
from test_phase3a import assess
from intelligence_fixtures import bundle, enriched, event


@pytest.fixture
def engine(tmp_path):
    return Phase3AEngine(Store(tmp_path / "forensic.sqlite3"),
                         intelligence_policy=IntelligencePolicy(allow_fixture_data=True))


@pytest.mark.parametrize("interval", ["5min", "15min", "1h", "4h"])
def test_xau_candles_must_align_to_their_actual_timeframe(interval):
    rows = frame(interval)
    for row in rows:
        row["t"] = stamp(parse(row["t"]) - timedelta(minutes=1))
    assert f"INVALID_DATA:{interval}" in closed_frame(rows, interval, NOW)[1]


def test_bad_forming_minute_cannot_block_a_known_stop(engine):
    data = enriched()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0) + timedelta(minutes=minute))
    engine.tick(data, NOW + timedelta(minutes=2))
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=2), low=4330)
    data["1min"].append({"t": stamp(NOW.replace(second=0) + timedelta(minutes=3)), "c": None})
    engine.tick(data, NOW + timedelta(minutes=3))
    assert active(engine) is None
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1


@pytest.mark.parametrize("key", ["auth", "sig", "credential", "authorization", "access_token"])
def test_credential_query_aliases_are_rejected(key):
    with pytest.raises(ValueError):
        source_url(f"https://example.invalid/article?{key}=NON_SECRET_TEST_MARKER")


def test_record_fixture_marker_cannot_be_erased_by_live_envelope():
    data = bundle()
    data["news"]["data_mode"] = "LIVE"
    data["news"]["records"][0]["data_mode"] = "FIXTURE"
    assert normalize_bundle(data, NOW)["news"]["data_mode"] == "FIXTURE"


def test_registered_fixture_adapter_cannot_self_relabel_its_payload_live():
    class Mislabelled:
        name = "registered-test-adapter"
        data_mode = "FIXTURE"
        def fetch(self, now):
            return dict(bundle()["news"], data_mode="LIVE")
    result = IntelligenceFeed({"news": Mislabelled()}, clock=lambda: NOW).fetch(NOW)
    assert result["news"]["data_mode"] == "FIXTURE"


def test_partial_unavailable_bundle_cannot_be_aggregated_as_live():
    data = bundle()
    for value in data.values():
        value["data_mode"] = "LIVE"  # Synthetic input for provenance-contract test only.
    del data["calendar"]
    result = assess(data)
    assert result["status"] == "BLOCKED" and result["data_mode"] != "LIVE"


def test_real_mode_api_cannot_serve_previously_allowed_fixture_signal(engine):
    decision = engine.tick(enriched(), NOW)
    assert decision["direction"] == "BUY"
    with TestClient(create_phase3a_app(db_path=engine.store.path, start_worker=False, clock=lambda: NOW)) as client:
        result = client.get("/signal").json()
        assert result["direction"] == "NO_TRADE"
        assert "FIXTURE_DATA_DISABLED" in result["veto_codes"]


def test_replay_detects_execution_context_tamper_even_if_direction_unchanged(engine):
    decision = engine.tick(enriched(), NOW)
    with engine.store.transaction() as conn:
        conn.execute("UPDATE decisions SET payload=json_set(payload, '$.execution_context.daily_loss_r', 100)")
    with pytest.raises(ValueError, match="Decision checksum"):
        engine.replay(decision["decision_id"])


def test_same_publisher_equal_time_news_revision_conflict_is_not_arbitrary():
    data = bundle()
    first = data["news"]["records"][0]
    data["news"]["records"].append(dict(first, title="Gold falls as dollar rises"))
    assert "CONFLICTING_NEWS_REVISION" in assess(data)["vetoes"]


def test_syndication_does_not_refresh_old_story_conflict_weight():
    data = bundle()
    first = data["news"]["records"][0]
    first["published_at"] = stamp(NOW - timedelta(hours=2))
    data["news"]["records"].append(dict(first, source="syndicate", published_at=stamp(NOW - timedelta(seconds=10))))
    opposite = dict(first, id="different", url="https://fixtures.invalid/different", source="different",
                    published_at=stamp(NOW - timedelta(seconds=10)), title="Gold falls as dollar rises")
    data["news"]["records"].append(opposite)
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:news" not in assess(data)["vetoes"]


def test_old_calendar_conflict_outside_risk_horizon_does_not_block_today():
    data = bundle()
    old = event(minutes=-10000)
    data["calendar"]["records"] = [old, dict(old, source="other", status="CANCELLED")]
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:calendar" not in assess(data)["vetoes"]


def test_story_cannot_be_rejuvenated_after_original_leaves_next_feed_batch(engine):
    first = bundle()
    first["news"]["records"][0]["published_at"] = stamp(NOW - timedelta(hours=2))
    original = engine.tick(enriched(intelligence=first), NOW)
    later = NOW + timedelta(minutes=5)
    latest = bundle(later)
    latest["news"]["records"][0].update(id="syndicated-new-id", source="syndicate",
                                           url="https://fixtures.invalid/syndicate/story")
    revised = Phase3AEngine(engine.store, intelligence_policy=IntelligencePolicy(allow_fixture_data=True)).tick(
        enriched(later, latest), later)
    assert revised["intelligence_components"]["news_sentiment_score"] < original["intelligence_components"]["news_sentiment_score"]
    assert engine.replay(original["decision_id"])["matches"]
    assert engine.replay(revised["decision_id"])["matches"]


def test_unknown_registered_provider_cannot_claim_live():
    class Unknown:
        name = "unknown"
        def fetch(self, now):
            return dict(bundle()["news"], data_mode="LIVE")
    assert IntelligenceFeed({"news": Unknown()}, clock=lambda: NOW).fetch(NOW)["news"]["status"] == "UNAVAILABLE"


def test_synthetic_gold_cannot_be_hidden_by_live_intelligence(tmp_path):
    data = enriched()
    for envelope in data["__intelligence__"].values():
        envelope["data_mode"] = "LIVE"
    result = Phase3AEngine(Store(tmp_path / "strict.sqlite3")).tick(data, NOW)
    assert result["direction"] == "NO_TRADE"
    assert "FIXTURE_DATA_DISABLED" in result["veto_codes"]
    assert result["intelligence"]["data_mode"] == "FIXTURE"


@pytest.mark.parametrize("defect", ["missing", "future", "unknown"])
def test_unverified_gold_provenance_fails_closed(engine, defect):
    data = enriched()
    if defect == "missing":
        del data[MARKET_KEY]
    elif defect == "future":
        data[MARKET_KEY]["retrieved_at"] = stamp(NOW + timedelta(seconds=1))
    else:
        data[MARKET_KEY]["data_mode"] = "UNKNOWN"
    result = engine.tick(data, NOW)
    assert "UNVERIFIED_XAU_PROVENANCE" in result["veto_codes"]


def test_real_prices_cannot_close_a_fixture_position(engine):
    data = enriched()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0) + timedelta(minutes=minute))
    engine.tick(data, NOW + timedelta(minutes=2))
    before = active(engine)
    data[MARKET_KEY]["data_mode"] = "LIVE"
    for envelope in data["__intelligence__"].values():
        envelope["data_mode"] = "LIVE"
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=2), low=4330)
    result = engine.tick(data, NOW + timedelta(minutes=3))
    assert "XAU_MONITOR_PROVENANCE_MISMATCH" in result["veto_codes"]
    assert "INPUT_MODE_DATABASE_MISMATCH" in result["veto_codes"]
    assert active(engine) == before


def test_per_channel_modes_have_distinct_calibration_identity(tmp_path):
    identities = []
    for index, channel in enumerate(("usd", "news")):
        data = enriched()
        data["__intelligence__"][channel]["data_mode"] = "DELAYED"
        engine = Phase3AEngine(Store(tmp_path / f"m{index}.sqlite3"), intelligence_policy=IntelligencePolicy(allow_fixture_data=True))
        identities.append(engine.tick(data, NOW)["model_identity"])
    assert identities[0] != identities[1]


def test_old_same_publisher_event_revision_does_not_veto_today():
    data = bundle()
    old = event(minutes=-10000)
    data["calendar"]["records"] = [old, dict(old, status="CANCELLED")]
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:calendar" not in assess(data)["vetoes"]


def test_conflict_detection_is_linear_in_record_count():
    class Counted(dict):
        reads = 0
        def __getitem__(self, key):
            Counted.reads += 1
            return super().__getitem__(key)
    rows = [Counted(source=str(i), gold_impact=.8) for i in range(1000)]
    assert not conflict(rows)
    assert Counted.reads <= 4000


def test_timezone_offsets_produce_identical_source_audits():
    from datetime import timezone
    raw = bundle()
    baseline = assess(raw)
    def convert(value):
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        if isinstance(value, str) and value.endswith("+00:00"):
            return parse(value).astimezone(timezone(timedelta(hours=-4))).isoformat()
        return value
    assert assess(convert(raw)) == baseline


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_opposing_intelligence_cannot_reverse_strong_market_evidence(engine, direction):
    data = enriched()
    if direction == "SELL":
        for interval in ("1min", "5min", "15min", "1h", "4h"):
            for row in data[interval]:
                row.update(o=9000-row["o"], c=9000-row["c"], h=9000-row["l"], l=9000-row["h"])
    else:
        intelligence = data["__intelligence__"]
        for channel in ("usd", "yields"):
            for row in intelligence[channel]["records"]:
                if row["id"].endswith("latest"):
                    row["value"] += 1
        intelligence["macro"]["records"][0]["expected_rate"] = 5
        intelligence["news"]["records"][0]["title"] = "Gold falls as dollar rises"
    result = engine.tick(data, NOW)
    side, opposite = ("buy_score", "sell_score") if direction == "BUY" else ("sell_score", "buy_score")
    assert result["technical_scores"][side] - result["technical_scores"][opposite] > 50
    assert result["combined_scores"][side] > result["combined_scores"][opposite]
    assert result["direction"] in (direction, "NO_TRADE")


def test_delayed_adapter_and_record_cannot_promote_live():
    class Delayed:
        name = "delayed-contract-fixture"
        data_mode = "DELAYED"
        def fetch(self, now):
            return dict(bundle()["news"], data_mode="LIVE")
    assert IntelligenceFeed({"news": Delayed()}, clock=lambda: NOW).fetch(NOW)["news"]["data_mode"] == "DELAYED"
    data = bundle()
    data["news"]["data_mode"] = "LIVE"
    data["news"]["records"][0]["is_synthetic"] = True
    assert normalize_bundle(data, NOW)["news"]["data_mode"] == "FIXTURE"
