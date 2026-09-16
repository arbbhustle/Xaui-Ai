"""Offline macro/news integration, provenance and point-in-time regressions."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from backend.domain import Policy, parse, stamp
from backend.intelligence import IntelligencePolicy, EVENT_WINDOWS, assess_intelligence, news_score
from backend.intelligence_providers import (BUNDLE_KEY, CHANNELS, IntelligenceFeed, IntelligenceMarketFeed,
                                           normalize_bundle, source_url)
from backend.phase2 import Phase2Engine
from backend.phase3a import Phase3AEngine, SOURCE_FILES, implementation_hash
from backend.phase3a_main import create_phase3a_app
from backend.storage import Store
from backend.worker import Monitor
from test_phase1 import NOW, frames, active, append_minute
from intelligence_fixtures import bundle, enriched, record, event, FixtureProvider

FIXTURE_POLICY = IntelligencePolicy(allow_fixture_data=True)


@pytest.fixture
def engine(tmp_path):
    return Phase3AEngine(Store(tmp_path / "phase3a.sqlite3"), intelligence_policy=FIXTURE_POLICY)


def assess(data=None, now=NOW, policy=FIXTURE_POLICY):
    return assess_intelligence(normalize_bundle(bundle() if data is None else data, now), now, policy)


def test_default_missing_credentials_do_not_generate_data(tmp_path):
    feed = IntelligenceFeed(clock=lambda: NOW)
    values = feed.fetch(NOW)
    assert all(x["status"] == "UNAVAILABLE" and x["records"] == [] for x in values.values())
    engine = Phase3AEngine(Store(tmp_path / "missing.sqlite3"))
    result = engine.tick(frames(), NOW)
    assert result["direction"] == "NO_TRADE"
    assert result["intelligence"]["data_mode"] == "UNAVAILABLE"
    assert all(value is None for value in result["intelligence_components"].values())
    assert "MISSING_CRITICAL_EVENT_DATA" in result["veto_codes"]
    assert active(engine) is None
    assert engine.replay(result["decision_id"])["matches"]


def test_fixture_mode_requires_explicit_opt_in_and_never_claims_live(tmp_path):
    engine = Phase3AEngine(Store(tmp_path / "default.sqlite3"))
    result = engine.tick(enriched(), NOW)
    assert "FIXTURE_DATA_DISABLED" in result["veto_codes"]
    assert result["data_status"] != "LIVE_DATA"
    raw = bundle()["news"]
    raw["data_mode"] = "LIVE"
    feed = IntelligenceFeed({"news": FixtureProvider(raw)}, clock=lambda: NOW)
    assert feed.fetch(NOW)["news"]["data_mode"] == "FIXTURE"


def test_all_components_provenance_and_technical_scores_remain_visible(engine):
    result = engine.tick(enriched(), NOW)
    assert result["direction"] == "BUY"
    assert result["data_status"] == "FIXTURE_DATA"
    assert set(result["intelligence_components"]) == {"usd_score", "yields_score", "macro_score", "news_sentiment_score", "event_risk_score"}
    assert len(result["components"]) == 5 and len(result["timeframes"]) == 5
    assert result["technical_scores"]["buy_score"] == pytest.approx(94.7528)
    assert result["combined_scores"]["buy_score"] == result["buy_score"]
    for source in result["intelligence"]["sources"]:
        assert source["provider"] and source["source_timestamp"] and source["retrieved_at"]
        assert source["fresh"] and source["age_seconds"] >= 0
        assert all(r["source"] and r["url"] and r["published_at"] for r in source["records"])
    assert engine.replay(result["decision_id"])["matches"]


@pytest.mark.parametrize("channel", ["usd", "yields"])
@pytest.mark.parametrize("rising", [True, False])
def test_usd_and_yields_momentum_and_inverse_gold_impact(channel, rising):
    data = bundle()
    for row in data[channel]["records"]:
        if "latest" in row["id"]:
            row["value"] += (1 if channel == "usd" else .2) if rising else 0
    result = assess(data)
    items = result["details"][channel]
    assert all(item["direction"] == ("UP" if rising else "DOWN") for item in items)
    assert all(item["gold_impact"] < 0 if rising else item["gold_impact"] > 0 for item in items)
    assert all(item["anchor_at"] < item["source_at"] for item in items)


@pytest.mark.parametrize("instrument", ["US2Y", "US10Y"])
def test_both_treasury_tenors_are_required(instrument):
    data = bundle()
    data["yields"]["records"] = [r for r in data["yields"]["records"] if r["instrument"] != instrument]
    assert "MISSING_CRITICAL_INTELLIGENCE:yields" in assess(data)["vetoes"]


def test_usd_proxy_is_explicitly_named():
    data = bundle()
    for row in data["usd"]["records"]:
        row["instrument"] = "USD_BROAD"
    result = assess(data)
    assert result["details"]["usd"][0]["instrument"] == "USD_BROAD"


def test_missing_optional_rate_expectations_are_not_invented(engine):
    data = bundle()
    data["macro"] = {"status": "UNAVAILABLE", "provider": "not-configured", "retrieved_at": stamp(NOW)}
    result = engine.tick(enriched(intelligence=data), NOW)
    assert result["intelligence_components"]["macro_score"] is None
    assert "macro_score" not in result["blend_weights"]
    assert result["direction"] == "BUY"
    assert any("no macro value inferred" in r for r in result["reasons"])


@pytest.mark.parametrize("channel", CHANNELS)
def test_stale_envelopes_are_hard_vetoes(channel):
    data = bundle()
    data[channel]["as_of"] = stamp(NOW - timedelta(days=2))
    # Preserve logically ordered old records so this tests staleness, not future data.
    for row in data[channel]["records"]:
        row["published_at"] = data[channel]["as_of"]
        if "observed_at" in row:
            row["observed_at"] = data[channel]["as_of"]
    result = assess(data)
    assert f"STALE_INTELLIGENCE:{channel}" in result["vetoes"]


def test_retrieval_cannot_refresh_old_market_observations():
    data = bundle()
    for row in data["usd"]["records"]:
        row["observed_at"] = stamp(parse(row["observed_at"]) - timedelta(hours=1))
    result = assess(data)
    assert "STALE_INTELLIGENCE:usd:DXY" in result["vetoes"]


@pytest.mark.parametrize("channel", CHANNELS)
def test_future_publication_is_never_used(channel):
    data = bundle()
    data[channel]["records"][0]["published_at"] = stamp(NOW + timedelta(seconds=1))
    result = assess(data)
    assert f"FUTURE_INTELLIGENCE:{channel}" in result["vetoes"]
    assert channel not in result["details"]


@pytest.mark.parametrize("field", ["as_of", "retrieved_at"])
def test_future_envelope_clock_is_rejected(field):
    data = bundle()
    data["news"][field] = stamp(NOW + timedelta(seconds=1))
    assert "FUTURE_INTELLIGENCE:news" in assess(data)["vetoes"]


@pytest.mark.parametrize("kind", ["CPI", "NFP", "PCE", "FOMC"])
@pytest.mark.parametrize("boundary", ["pre", "during", "post", "outside"])
def test_event_blackout_boundaries(kind, boundary):
    pre, post = EVENT_WINDOWS[kind]
    minutes = {"pre": pre, "during": 0, "post": -post, "outside": -post - 1}[boundary]
    data = bundle()
    data["calendar"]["records"] = [event(kind=kind, minutes=minutes)]
    result = assess(data)
    relevant = [v for v in result["vetoes"] if v.startswith("MAJOR_EVENT")]
    assert bool(relevant) == (boundary != "outside")
    if boundary != "outside":
        assert result["scores"]["event_risk_score"] == 100
    assert result["details"]["calendar"][0]["seconds_until"] == minutes * 60


def test_fomc_press_conference_duration_extends_post_window():
    data = bundle()
    item = event(kind="FOMC", minutes=-90)
    item["end_at"] = stamp(NOW - timedelta(minutes=30))
    data["calendar"]["records"] = [item]
    assert "MAJOR_EVENT_BLACKOUT" in assess(data)["vetoes"]


def test_irrelevant_ancient_event_cannot_crash_current_monitoring(engine):
    data = bundle()
    item = data["calendar"]["records"][0]
    item.update(scheduled_at="0001-01-01T00:00:00+00:00", end_at="0001-01-01T00:00:00+00:00")
    result = engine.tick(enriched(intelligence=data), NOW)
    assert result["intelligence_components"]["event_risk_score"] == 0
    assert engine.replay(result["decision_id"])["matches"]


def test_sources_disagreeing_on_importance_cannot_downgrade_event():
    data = bundle()
    high = event(kind="OTHER", minutes=20)
    low = dict(high, source="other-calendar", importance="LOW")
    data["calendar"]["records"] = [high, low]
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:calendar" in assess(data)["vetoes"]


def test_source_observation_staleness_is_reflected_in_freshness_audit():
    data = bundle()
    for row in data["usd"]["records"]:
        row["observed_at"] = stamp(parse(row["observed_at"]) - timedelta(hours=1))
    source = next(s for s in assess(data)["sources"] if s["channel"] == "usd")
    assert source["feed_fresh"] and not source["fresh"]


@pytest.mark.parametrize("defect", ["missing", "partial_types", "short_future", "short_past", "not_complete"])
def test_incomplete_calendar_cannot_mean_no_event_risk(defect):
    data = bundle()
    if defect == "missing":
        del data["calendar"]
    elif defect == "partial_types":
        data["calendar"]["coverage"]["event_types"].remove("PCE")
    elif defect == "short_future":
        data["calendar"]["coverage"]["end"] = stamp(NOW + timedelta(hours=1))
    elif defect == "short_past":
        data["calendar"]["coverage"]["start"] = stamp(NOW)
    else:
        data["calendar"]["coverage"]["complete"] = False
    assert "MISSING_CRITICAL_EVENT_DATA" in assess(data)["vetoes"]


def test_verified_empty_calendar_and_news_are_not_missing():
    data = bundle()
    data["calendar"]["records"] = []
    data["news"]["records"] = []
    result = assess(data)
    assert result["status"] == "READY"
    assert result["scores"]["event_risk_score"] == result["scores"]["news_sentiment_score"] == 0


def test_conflicting_calendars_and_latest_known_revision():
    data = bundle()
    first = event(minutes=180)
    revised = dict(first, published_at=stamp(NOW - timedelta(minutes=10)), scheduled_at=stamp(NOW + timedelta(minutes=20)), end_at=stamp(NOW + timedelta(minutes=20)))
    data["calendar"]["records"] = [first, revised]
    assert "MAJOR_EVENT_IMMINENT" in assess(data)["vetoes"]
    opposing = dict(first, source="other-calendar")
    data["calendar"]["records"].append(opposing)
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:calendar" in assess(data)["vetoes"]


def test_future_event_revision_cannot_erase_known_risk():
    data = bundle()
    first = event(minutes=10)
    revised = dict(first, published_at=stamp(NOW + timedelta(minutes=1)), status="CANCELLED")
    data["calendar"]["records"] = [first, revised]
    result = assess(data)
    assert result["status"] == "BLOCKED"
    assert "FUTURE_INTELLIGENCE:calendar" in result["vetoes"]


@pytest.mark.parametrize("channel", ["usd", "yields", "macro"])
def test_conflicting_high_impact_numeric_sources_veto(channel):
    data = bundle()
    other = deepcopy(data[channel]["records"])
    for row in other:
        row["source"] = "opposing-source"
        if channel == "macro":
            row["expected_rate"] = 5
        elif "latest" in row["id"]:
            row["value"] += 2 if channel == "usd" else .4
    data[channel]["records"] += other
    assert f"CONFLICTING_HIGH_IMPACT_SOURCES:{channel}" in assess(data)["vetoes"]


def test_news_deduplication_keeps_all_sources_without_inflating_score():
    data = bundle()
    original = assess(data)
    duplicate = dict(data["news"]["records"][0], id="syndicated", source="syndicate", url="https://fixtures.invalid/syndicate/story")
    data["news"]["records"].append(duplicate)
    result = assess(data)
    assert result["scores"]["news_sentiment_score"] == original["scores"]["news_sentiment_score"]
    assert len(result["details"]["news"]) == 1
    assert len(result["details"]["news"][0]["sources"]) == 2


def test_news_relevance_sentiment_decay_and_syndication_does_not_rejuvenate():
    data = bundle()
    article = data["news"]["records"][0]
    fresh = assess(data)["scores"]["news_sentiment_score"]
    article["published_at"] = stamp(NOW - timedelta(hours=1))
    old = assess(data)["scores"]["news_sentiment_score"]
    assert old < fresh
    syndicated = dict(article, source="syndicate", published_at=stamp(NOW - timedelta(seconds=10)))
    data["news"]["records"].append(syndicated)
    assert assess(data)["scores"]["news_sentiment_score"] == old
    article["title"] = "Local football team wins match"
    assert news_score([article], NOW, FIXTURE_POLICY)[0] == 0
    article["title"] = "Unconfirmed reports that gold rises as dollar falls"
    assert news_score([article], NOW, FIXTURE_POLICY)[0] == 0


def test_conflicting_high_impact_news_veto():
    data = bundle()
    data["news"]["records"].append(record("hawkish", "opposing-news", NOW - timedelta(seconds=60),
                                           title="Gold falls as dollar rises", importance="HIGH"))
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:news" in assess(data)["vetoes"]


def test_news_dedup_does_not_hide_conflicting_reports_of_same_story():
    data = bundle()
    first = data["news"]["records"][0]
    data["news"]["records"].append(dict(first, source="other-news", title="Gold falls as dollar rises"))
    result = assess(data)
    assert len(result["details"]["news"]) == 1
    assert "CONFLICTING_HIGH_IMPACT_SOURCES:news" in result["vetoes"]


@pytest.mark.parametrize("channel", ["macro", "calendar"])
def test_conflicting_equal_timestamp_revisions_fail_closed(channel):
    data = bundle()
    other = dict(data[channel]["records"][0])
    if channel == "macro":
        other["expected_rate"] = 5
    else:
        other["status"] = "CANCELLED"
    data[channel]["records"].append(other)
    assert f"CONFLICTING_HIGH_IMPACT_SOURCES:{channel}" in assess(data)["vetoes"]


def test_source_order_does_not_change_intelligence_result():
    data = bundle()
    original = assess(data)
    for source in data.values():
        source["records"].reverse()
    assert assess(data) == original


def test_old_archived_news_cannot_dilute_current_news():
    data = bundle()
    score = assess(data)["scores"]["news_sentiment_score"]
    data["news"]["records"].append(record("old", "archive", NOW - timedelta(days=1),
        title="Gold drops on hawkish Fed rate hike", importance="HIGH"))
    assert assess(data)["scores"]["news_sentiment_score"] == score


def test_slow_provider_has_bounded_wait_and_no_duplicate_inflight_requests():
    import threading
    import time
    release = threading.Event()
    calls = []
    class Slow:
        name = "slow-fixture-provider"
        def fetch(self, now):
            calls.append(1)
            release.wait(3)
            return bundle()["news"]
    feed = IntelligenceFeed({"news": Slow()}, clock=lambda: NOW, timeout_seconds=.01)
    try:
        started = time.monotonic()
        assert feed.fetch(NOW)["news"]["status"] == "UNAVAILABLE"
        assert feed.fetch(NOW)["news"]["status"] == "UNAVAILABLE"
        assert time.monotonic() - started < 1
        assert len(calls) == 1
    finally:
        release.set()


def test_failed_configured_macro_provider_is_not_optional_missing_data():
    data = bundle()
    data["macro"] = {"provider": "failed-macro", "retrieved_at": stamp(NOW), "status": "UNAVAILABLE", "error": "PROVIDER_FAILED"}
    assert "MACRO_PROVIDER_FAILURE" in assess(data)["vetoes"]


def test_configured_macro_unavailable_status_also_fails_closed():
    data = bundle()
    data["macro"] = {"provider": "configured-macro", "retrieved_at": stamp(NOW), "status": "UNAVAILABLE"}
    assert "MACRO_PROVIDER_FAILURE" in assess(data)["vetoes"]


def test_provider_failure_is_isolated_and_secret_safe(caplog):
    class Broken:
        name = "broken-provider"
        def fetch(self, now):
            raise RuntimeError("https://example.invalid?apikey=SECRET_TEST_ONLY")
    feed = IntelligenceFeed({"news": Broken(), "usd": FixtureProvider(bundle()["usd"])}, clock=lambda: NOW)
    result = feed.fetch(NOW)
    assert result["news"]["status"] == "UNAVAILABLE" and result["usd"]["status"] == "OK"
    assert "SECRET_TEST_ONLY" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:pass@example.invalid", "https://example.invalid?api_key=SECRET"])
def test_trace_urls_cannot_contain_credentials(url):
    with pytest.raises(ValueError):
        source_url(url)


def test_news_tracking_parameters_are_canonicalized():
    assert source_url("https://example.invalid/article?id=5&utm_source=x#section") == "https://example.invalid/article?id=5"


def test_macro_outage_and_nan_cannot_freeze_open_trade_exit(engine):
    data = enriched()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0) + timedelta(minutes=minute))
    engine.tick(data, NOW + timedelta(minutes=2))
    assert active(engine)["status"] == "OPEN"
    data[BUNDLE_KEY]["usd"]["records"][-1]["value"] = float("nan")
    data[BUNDLE_KEY]["news"] = None
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=2), low=4330)
    result = engine.tick(data, NOW + timedelta(minutes=3))
    assert active(engine) is None and result["direction"] == "NO_TRADE"
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1


def test_later_event_veto_does_not_erase_earlier_fill_and_stop(engine):
    data = enriched()
    engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), low=4330)
    data[BUNDLE_KEY]["calendar"]["records"] = [event(minutes=10)]
    result = engine.tick(data, NOW + timedelta(minutes=2))
    assert "MAJOR_EVENT_IMMINENT" in result["veto_codes"]
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1


def test_replay_uses_original_sources_not_revised_feed(engine, tmp_path):
    data = enriched()
    decision = engine.tick(data, NOW)
    data[BUNDLE_KEY]["news"]["records"][0]["title"] = "Gold falls as dollar rises"
    later = NOW + timedelta(minutes=5)
    engine.tick(enriched(later), later)
    restarted = Phase3AEngine(Store(engine.store.path))
    assert restarted.replay(decision["decision_id"])["matches"]
    backup = tmp_path / "backup.sqlite3"
    engine.store.backup(backup)
    assert Phase3AEngine(Store(backup)).replay(decision["decision_id"])["matches"]


def test_response_rechecks_news_freshness_without_db_writes(engine):
    data = bundle()
    data["news"]["as_of"] = stamp(NOW - timedelta(seconds=850))
    data["news"]["records"][0]["published_at"] = stamp(NOW - timedelta(seconds=860))
    engine.tick(enriched(intelligence=data), NOW)
    now = [NOW]
    with engine.store.connect() as conn:
        before = list(conn.iterdump())
    with TestClient(create_phase3a_app(db_path=engine.store.path, start_worker=False,
                                     intelligence_policy=FIXTURE_POLICY, clock=lambda: now[0])) as client:
        now[0] += timedelta(seconds=51)
        result = client.get("/signal").json()
        assert "STALE_INTELLIGENCE:news" in result["veto_codes"]
        assert result["direction"] == "NO_TRADE" and result["data_status"] != "LIVE_DATA"
        assert client.get("/health").status_code == 503
    with engine.store.connect() as conn:
        assert before == list(conn.iterdump())


def test_old_decision_intelligence_expires_even_when_worker_health_is_fresh(engine):
    data = bundle()
    data["news"]["as_of"] = stamp(NOW - timedelta(seconds=850))
    data["news"]["records"][0]["published_at"] = stamp(NOW - timedelta(seconds=860))
    engine.tick(enriched(intelligence=data), NOW)
    later = NOW + timedelta(minutes=1)
    engine.tick(enriched(later), later)
    with TestClient(create_phase3a_app(db_path=engine.store.path, start_worker=False,
                    intelligence_policy=FIXTURE_POLICY, clock=lambda: later)) as client:
        assert client.get("/health").status_code == 200
        result = client.get("/signal").json()
        assert result["direction"] == "NO_TRADE" and "STALE_INTELLIGENCE:news" in result["veto_codes"]


def test_fixture_outcomes_cannot_calibrate_delayed_or_live_inputs(engine):
    data = enriched()
    engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), high=4344)
    engine.tick(data, NOW + timedelta(minutes=2))
    later = NOW + timedelta(minutes=5)
    known = engine.tick(enriched(later), later)
    assert known["calibration"]["sample_count"] == 1
    assert "DUPLICATE_SIGNAL_EPISODE" in known["veto_codes"]
    newest = NOW + timedelta(minutes=10)
    different_mode = enriched(newest)
    # Synthetic contract input to test mode isolation; never used outside tests.
    for source in different_mode[BUNDLE_KEY].values():
        source["data_mode"] = "DELAYED"
    result = engine.tick(different_mode, newest)
    assert result["calibration"]["sample_count"] == 0
    assert result["model_identity"] != known["model_identity"]
    assert engine.replay(result["decision_id"])["matches"]


def test_response_enters_event_blackout_between_worker_ticks(engine):
    data = bundle()
    data["calendar"]["records"] = [event(minutes=60.5)]
    engine.tick(enriched(intelligence=data), NOW)
    with TestClient(create_phase3a_app(db_path=engine.store.path, start_worker=False,
                    intelligence_policy=FIXTURE_POLICY, clock=lambda: NOW + timedelta(seconds=31))) as client:
        result = client.get("/signal").json()
        assert "MAJOR_EVENT_IMMINENT" in result["veto_codes"] and result["direction"] == "NO_TRADE"


def test_phase2_database_is_refused_unchanged(tmp_path):
    store = Store(tmp_path / "phase2.sqlite3")
    Phase2Engine(store).tick(frames(), NOW)
    with store.connect() as conn:
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match="separate demo database"):
        Phase3AEngine(store)
    with store.connect() as conn:
        assert before == list(conn.iterdump())


def test_phase3a_refuses_render_and_existing_phase2_path(monkeypatch):
    monkeypatch.setenv("PHASE2_DEMO_DB_PATH", "backend/data/phase2.sqlite3")
    with pytest.raises(ValueError, match="own demo database"):
        create_phase3a_app(db_path="backend/data/phase2.sqlite3")
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="local-only"):
        create_phase3a_app()


def test_worker_acquires_intelligence_without_http_requests(engine):
    class Market:
        name = "fixture-market"
        data_mode = "FIXTURE"
        def fetch(self, now):
            return frames(), {}
    providers = {k: FixtureProvider(v) for k, v in bundle().items()}
    feed = IntelligenceMarketFeed(Market(), IntelligenceFeed(providers, clock=lambda: NOW))
    Monitor(engine, feed, clock=lambda: NOW).run_once()
    assert active(engine)["status"] == "PENDING"


def test_source_hash_portability_and_snapshot_tampering(engine, tmp_path):
    decision = engine.tick(enriched(), NOW)
    root = Path(__file__).parents[1]
    for name in SOURCE_FILES:
        (tmp_path / name).write_bytes((root / name).read_text(encoding="utf-8").replace("\n", "\r\n").encode())
    assert implementation_hash(tmp_path) == engine.code_hash
    with engine.store.transaction() as conn:
        conn.execute("UPDATE snapshots SET payload=json_set(payload, '$.intelligence.news.records[0].title', 'altered')")
    with pytest.raises(ValueError, match="checksum"):
        engine.replay(decision["decision_id"])


def test_phase3a_cli_replay_dispatch(engine, monkeypatch, capsys):
    from backend.manage import main
    decision = engine.tick(enriched(), NOW)
    monkeypatch.setattr("sys.argv", ["manage", "--database", engine.store.path, "replay", decision["decision_id"]])
    main()
    assert json.loads(capsys.readouterr().out)["matches"]


@pytest.mark.parametrize("bad", [dict(usd_max_age=0), dict(news_max_age=float("nan")),
                                dict(allow_fixture_data="true"), dict(calendar_lookahead_hours=1)])
def test_invalid_policy_rejected(bad):
    with pytest.raises(ValueError):
        IntelligencePolicy(**bad)
