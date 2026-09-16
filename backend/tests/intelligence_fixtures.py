"""Synthetic, explicitly FIXTURE-labelled inputs. Never imported by the service."""
from copy import deepcopy
from datetime import timedelta

from backend.domain import stamp
from backend.intelligence_providers import CHANNELS, CRITICAL_EVENTS, BUNDLE_KEY, MARKET_KEY
from test_phase1 import NOW, frames


def record(identifier, source, published_at, **fields):
    return {"id": identifier, "source": source, "url": f"https://fixtures.invalid/{source}/{identifier}",
            "published_at": stamp(published_at), **fields}


def bundle(now=NOW):
    result = {channel: {"provider": "fixture-" + channel, "status": "OK", "data_mode": "FIXTURE",
                        "as_of": stamp(now - timedelta(seconds=5)), "retrieved_at": stamp(now), "records": []}
              for channel in CHANNELS}
    for channel, instruments in (("usd", [("DXY", 100, 99.7)]),
                                 ("yields", [("US2Y", 4.25, 4.20), ("US10Y", 4.0, 3.95)])):
        for instrument, before, after in instruments:
            for label, value, age in (("anchor", before, 3630), ("latest", after, 30)):
                at = now - timedelta(seconds=age)
                result[channel]["records"].append(record(instrument + label, "fixture-" + channel, at,
                    instrument=instrument, observed_at=stamp(at), value=value))
    result["macro"]["records"] = [record("rates", "fixture-macro", now - timedelta(hours=1),
        meeting_at=stamp(now + timedelta(days=7)), target_lower=4.25, target_upper=4.5, expected_rate=4.125)]
    result["calendar"]["coverage"] = {"complete": True, "country": "US", "event_types": list(CRITICAL_EVENTS),
                                     "start": stamp(now - timedelta(days=2)), "end": stamp(now + timedelta(days=3))}
    result["calendar"]["records"] = [event(now, "CPI", 2880)]
    result["news"]["records"] = [record("gold-headline", "fixture-news", now - timedelta(seconds=60),
        title="Gold rises as dollar falls", importance="HIGH")]
    return result


def event(now=NOW, kind="CPI", minutes=20, source="fixture-calendar"):
    scheduled = now + timedelta(minutes=minutes)
    return record(kind, source, now - timedelta(hours=1), event_key="US:" + kind + ":2026-09",
                  kind=kind, name=kind + " fixture event", scheduled_at=stamp(scheduled),
                  end_at=stamp(scheduled), importance="HIGH", status="SCHEDULED")


def enriched(now=NOW, intelligence=None):
    return {**frames(now), BUNDLE_KEY: bundle(now) if intelligence is None else intelligence,
            MARKET_KEY: {"provider": "fixture-xau", "data_mode": "FIXTURE", "retrieved_at": stamp(now)}}


class FixtureProvider:
    name = "deterministic-fixture"
    data_mode = "FIXTURE"

    def __init__(self, envelope):
        self.envelope = deepcopy(envelope)

    def fetch(self, now):
        # Even an accidentally relabelled test envelope cannot claim live acquisition.
        return dict(deepcopy(self.envelope), data_mode="FIXTURE")
