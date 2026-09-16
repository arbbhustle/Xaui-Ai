"""Slow gold regime contracts. No invented observations or network adapters."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from .domain import parse, stamp
from .intelligence_providers import IntelligenceFeed, number, source_url, text, timestamp

SLOW_KEY = "__slow_regime__"
CHANNELS = ("cot", "etf", "physical", "options")
MAX_AGE = {"cot": 10 * 86400, "etf": 3 * 86400, "physical": 7 * 86400, "options": 86400}


@dataclass(frozen=True)
class SlowPolicy:
    allow_test_data: bool = False

    def __post_init__(self):
        if type(self.allow_test_data) is not bool:
            raise ValueError("Invalid slow regime policy")


class SlowProvider(Protocol):
    """Score is a documented provider-normalized regime index, not a probability.

    observed_at is the economic observation; published_at is public availability.
    """
    name: str
    data_mode: str

    def fetch(self, now: datetime) -> dict: ...


def normalize_slow(raw, now):
    result = {}
    for channel in CHANNELS:
        empty = {"status": "UNAVAILABLE", "data_mode": "UNAVAILABLE", "records": [],
                 "provider": "not-configured", "error": None}
        if isinstance(raw, dict) and channel not in raw:
            result[channel] = empty
            continue
        try:
            envelope = raw[channel]
            provider = text(envelope["provider"])
            if envelope["status"] == "UNAVAILABLE":
                error = None if provider == "not-configured" and not envelope.get("error") else "PROVIDER_UNAVAILABLE"
                result[channel] = dict(empty, provider=provider, error=error)
                continue
            mode = envelope["data_mode"]
            if envelope["status"] != "OK" or mode not in ("LIVE", "DELAYED", "TEST_DATA"):
                raise ValueError("Invalid slow provenance")
            if "is_synthetic" in envelope and type(envelope["is_synthetic"]) is not bool:
                raise ValueError("Invalid synthetic marker")
            if envelope.get("is_synthetic") is True:
                mode = "TEST_DATA"
            rows = envelope["records"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
                raise ValueError("Invalid slow records")
            normalized = []
            for row in rows:
                if "is_synthetic" in row and type(row["is_synthetic"]) is not bool:
                    raise ValueError("Invalid synthetic marker")
                if "data_mode" in row and row["data_mode"] not in ("LIVE", "DELAYED", "FIXTURE", "TEST_DATA"):
                    raise ValueError("Invalid record provenance")
                if row.get("data_mode") in ("TEST_DATA", "FIXTURE") or row.get("is_synthetic") is True:
                    mode = "TEST_DATA"
                elif row.get("data_mode") == "DELAYED" and mode == "LIVE":
                    mode = "DELAYED"
                normalized.append({"source": text(row["source"]), "url": source_url(row["url"]),
                    "observed_at": timestamp(row["observed_at"]), "published_at": timestamp(row["published_at"]),
                    "score": number(row["score"], -100, 100), "method": text(row["method"])})
            result[channel] = {"status": "OK", "provider": provider, "data_mode": mode,
                "retrieved_at": timestamp(envelope["retrieved_at"]), "records": sorted(normalized, key=lambda r: (r["source"], r["published_at"], r["observed_at"], r["score"]))}
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            result[channel] = dict(empty, error="INVALID_SLOW_DATA")
    return result


def assess_slow(bundle, now, policy):
    audits, vetoes, scores = [], [], {}
    for channel in CHANNELS:
        envelope = bundle[channel]
        audit = {"channel": channel, **envelope, "fresh": False, "score": None}
        audits.append(audit)
        if envelope["status"] != "OK":
            if envelope["error"]:
                vetoes.append(f"INVALID_SLOW_FRESHNESS:{channel}")
            scores[channel] = None
            continue
        if envelope["data_mode"] == "TEST_DATA" and not policy.allow_test_data:
            vetoes.append("SLOW_TEST_DATA_DISABLED")
        latest = {}
        for row in envelope["records"]:
            source = row["source"]
            if not parse(row["observed_at"]) <= parse(row["published_at"]) <= parse(envelope["retrieved_at"]) <= now:
                vetoes.append(f"FUTURE_SLOW_DATA:{channel}")
            old = latest.get(source)
            if old and old["published_at"] == row["published_at"] and old != row:
                vetoes.append(f"CONFLICTING_SLOW_DATA:{channel}")
            if old is None or row["published_at"] > old["published_at"]:
                latest[source] = row
        if any((now - parse(r["observed_at"])).total_seconds() >= MAX_AGE[channel] or
               (now - parse(r["published_at"])).total_seconds() >= MAX_AGE[channel] for r in latest.values()):
            vetoes.append(f"INVALID_SLOW_FRESHNESS:{channel}")
        values = [r["score"] for r in latest.values()]
        if values and min(values) <= -60 and max(values) >= 60:
            vetoes.append(f"CONFLICTING_SLOW_DATA:{channel}")
        issues = [v for v in vetoes if v.endswith(":" + channel) or v == "SLOW_TEST_DATA_DISABLED"]
        audit["fresh"] = not issues
        audit["score"] = round(sum(values) / len(values), 4) if values and not issues else None
        scores[channel] = audit["score"]
    return {"role": "BACKGROUND_ONLY", "scores": scores, "sources": audits,
            "data_mode": "TEST_DATA" if any(x["data_mode"] == "TEST_DATA" for x in audits) else
                         "UNAVAILABLE" if all(x["status"] != "OK" for x in audits) else "SOURCE_DATA",
            "vetoes": sorted(set(vetoes)), "as_of": stamp(now)}


class SlowFeed(IntelligenceFeed):
    """Reuse bounded acquisition; successful envelopes require registered provenance."""
    channels = CHANNELS

    def _read(self, channel, provider, now, done, box):
        try:
            raw = provider.fetch(now)
            mode = getattr(provider, "data_mode", "UNAVAILABLE")
            if raw.get("status") == "OK" and mode not in ("LIVE", "DELAYED", "TEST_DATA"):
                raise ValueError("Unregistered slow provider")
            if mode == "TEST_DATA":
                raw = dict(raw, data_mode="TEST_DATA")
            elif mode == "DELAYED" and raw.get("data_mode") == "LIVE":
                raw = dict(raw, data_mode="DELAYED")
            raw = dict(raw, provider=text(provider.name), retrieved_at=stamp(self.clock()))
            box.append(normalize_slow({channel: raw}, now)[channel])
        except Exception:
            box.append({"status": "UNAVAILABLE", "data_mode": "UNAVAILABLE", "records": [],
                        "provider": "failed-provider", "error": "PROVIDER_UNAVAILABLE"})
        finally:
            done.set()


class Phase3BFeed:
    def __init__(self, market_feed, slow_feed):
        self.market_feed, self.slow_feed = market_feed, slow_feed

    def fetch(self, now):
        frames, errors = self.market_feed.fetch(now)
        return dict(frames, **{SLOW_KEY: self.slow_feed.fetch(now)}), errors
