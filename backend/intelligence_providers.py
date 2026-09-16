"""Provider contracts and isolated acquisition. No live provider is fabricated."""
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import math
import threading
import time

from .domain import canonical, parse, stamp

CHANNELS = ("usd", "yields", "macro", "calendar", "news")
CRITICAL_EVENTS = ("CPI", "NFP", "PCE", "FOMC")
BUNDLE_KEY = "__intelligence__"
MARKET_KEY = "__market_provenance__"


class IntelligenceProvider(Protocol):
    """Adapters must enforce bounded I/O and return a source-stamped envelope.

    fetch must not substitute retrieval time for the source's as_of timestamp.
    Adapters are trusted application code, never loaded from a URL or API input.
    """
    name: str
    data_mode: str

    def fetch(self, now: datetime) -> dict: ...


class UnavailableProvider:
    name = "not-configured"
    data_mode = "UNAVAILABLE"

    def fetch(self, now):
        return {"status": "UNAVAILABLE"}


def text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid text")
    return value.strip()


def timestamp(value):
    return stamp(parse(text(value, 60)))


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid numeric observation")
    return float(value)


def source_url(value):
    value = text(value, 2000)
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Invalid source URL")
    query = parse_qsl(url.query, keep_blank_values=True)
    if any(any(part in key.lower() for part in ("key", "token", "secret", "password", "sig", "auth", "credential")) for key, _ in query):
        raise ValueError("Credential-bearing source URL")
    query = sorted((k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid"))
    return urlunsplit(("https", url.netloc.lower(), url.path, urlencode(query), ""))


def normalize_envelope(channel, raw, retrieved_at, provider):
    """Whitelist fields before persistence. Invalid feed never contaminates others."""
    base = {"provider": text(provider), "retrieved_at": stamp(retrieved_at), "status": "UNAVAILABLE",
            "data_mode": "UNAVAILABLE", "as_of": None, "records": []}
    if not isinstance(raw, dict):
        raise ValueError("Invalid envelope")
    if raw.get("status") == "UNAVAILABLE":
        base["error"] = raw.get("error") if raw.get("error") in (
            "PROVIDER_FAILED", "INVALID_OR_MISSING_PROVIDER_DATA") else "PROVIDER_UNAVAILABLE"
        return base
    if raw.get("status") != "OK" or raw.get("data_mode") not in ("LIVE", "DELAYED", "FIXTURE", "TEST_DATA"):
        raise ValueError("Missing provenance mode")
    if "is_synthetic" in raw and type(raw["is_synthetic"]) is not bool:
        raise ValueError("Invalid synthetic marker")
    records = raw["records"]
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("Invalid record count")
    base.update(status="OK", data_mode=raw["data_mode"], as_of=timestamp(raw["as_of"]))
    if raw["data_mode"] == "TEST_DATA" or raw.get("is_synthetic") is True:
        base["data_mode"] = "FIXTURE"
    for row in records:
        if "is_synthetic" in row and type(row["is_synthetic"]) is not bool:
            raise ValueError("Invalid synthetic marker")
        if "data_mode" in row and row["data_mode"] not in ("LIVE", "DELAYED", "FIXTURE", "TEST_DATA"):
            raise ValueError("Invalid record provenance")
        if row.get("data_mode") in ("FIXTURE", "TEST_DATA") or row.get("is_synthetic") is True:
            base["data_mode"] = "FIXTURE"
        elif row.get("data_mode") == "DELAYED" and base["data_mode"] == "LIVE":
            base["data_mode"] = "DELAYED"
        item = {"id": text(row["id"]), "source": text(row["source"]),
                "url": source_url(row["url"]), "published_at": timestamp(row["published_at"])}
        if channel in ("usd", "yields"):
            instrument = text(row["instrument"])
            allowed = ("DXY", "USD_BROAD") if channel == "usd" else ("US2Y", "US10Y")
            if instrument not in allowed:
                raise ValueError("Unknown instrument")
            item.update(instrument=instrument, observed_at=timestamp(row["observed_at"]),
                        value=number(row["value"], .0001 if channel == "usd" else -5, 10000 if channel == "usd" else 30))
        elif channel == "macro":
            lower, upper = number(row["target_lower"], -5, 30), number(row["target_upper"], -5, 30)
            if lower > upper:
                raise ValueError("Invalid rate range")
            item.update(meeting_at=timestamp(row["meeting_at"]), target_lower=lower, target_upper=upper,
                        expected_rate=number(row["expected_rate"], -5, 30))
        elif channel == "calendar":
            kind = text(row["kind"])
            if kind not in (*CRITICAL_EVENTS, "OTHER") or row["importance"] not in ("HIGH", "MEDIUM", "LOW"):
                raise ValueError("Invalid event classification")
            if row["status"] not in ("SCHEDULED", "CANCELLED"):
                raise ValueError("Invalid event status")
            item.update(event_key=text(row["event_key"]), kind=kind, name=text(row["name"]),
                        scheduled_at=timestamp(row["scheduled_at"]), end_at=timestamp(row["end_at"]),
                        importance=row["importance"], status=row["status"])
            if not 0 <= (parse(item["end_at"]) - parse(item["scheduled_at"])).total_seconds() <= 14400:
                raise ValueError("Invalid event duration")
        else:
            if row["importance"] not in ("HIGH", "MEDIUM", "LOW"):
                raise ValueError("Invalid news importance")
            item.update(title=text(row["title"], 1000), importance=row["importance"])
        base["records"].append(item)
    if channel == "calendar":
        coverage = raw["coverage"]
        if (coverage.get("complete") is not True or coverage.get("country") != "US"
                or not set(CRITICAL_EVENTS).issubset(coverage["event_types"])):
            raise ValueError("Incomplete critical-event coverage")
        base["coverage"] = {"complete": True, "country": "US", "event_types": list(CRITICAL_EVENTS),
                            "start": timestamp(coverage["start"]), "end": timestamp(coverage["end"])}
    # Canonical order makes provider response ordering immaterial to scoring/hash.
    base["records"].sort(key=canonical)
    return base


def unavailable(channel, at, provider="not-configured", reason="PROVIDER_UNAVAILABLE"):
    return {"provider": provider, "retrieved_at": stamp(at), "status": "UNAVAILABLE",
            "data_mode": "UNAVAILABLE", "as_of": None, "records": [], "error": reason}


def normalize_bundle(bundle, now):
    result = {}
    for channel in CHANNELS:
        if isinstance(bundle, dict) and channel not in bundle:
            result[channel] = unavailable(channel, now)
            continue
        try:
            raw = bundle[channel]
            received = parse(raw["retrieved_at"])
            result[channel] = normalize_envelope(channel, raw, received, raw["provider"])
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            result[channel] = unavailable(channel, now, reason="INVALID_OR_MISSING_PROVIDER_DATA")
    return result


class IntelligenceFeed:
    channels = CHANNELS
    def __init__(self, providers=None, clock=lambda: datetime.now(timezone.utc), timeout_seconds=2):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 10:
            raise ValueError("Invalid provider acquisition budget")
        self.providers = providers or {}
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self._inflight = {}
        self._lock = threading.Lock()

    def _read(self, channel, provider, now, done, box):
        name = "unidentified-provider"
        try:
            name = text(provider.name)
            raw = provider.fetch(now)
            if raw.get("status") == "OK" and getattr(provider, "data_mode", None) not in ("LIVE", "DELAYED", "FIXTURE", "TEST_DATA"):
                raise ValueError("Unregistered provider provenance")
            if getattr(provider, "data_mode", None) in ("FIXTURE", "TEST_DATA"):
                raw = dict(raw, data_mode="FIXTURE")
            elif getattr(provider, "data_mode", None) == "DELAYED" and raw.get("data_mode") == "LIVE":
                raw = dict(raw, data_mode="DELAYED")
            box.append(normalize_envelope(channel, raw, self.clock(), name))
        except Exception:
            box.append(unavailable(channel, self.clock(), provider=name, reason="PROVIDER_FAILED"))
        finally:
            done.set()

    def fetch(self, now):
        # At most one in-flight call per channel. A stalled adapter cannot block
        # the minute monitor indefinitely or accumulate threads on every tick.
        with self._lock:
            deadline = time.monotonic() + self.timeout_seconds
            for channel in self.channels:
                if channel not in self._inflight:
                    done, box = threading.Event(), []
                    self._inflight[channel] = (done, box)
                    threading.Thread(target=self._read, args=(channel, self.providers.get(channel, UnavailableProvider()), now, done, box),
                                     daemon=True, name="intelligence-" + channel).start()
            result = {}
            for channel in self.channels:
                done, box = self._inflight[channel]
                if done.wait(max(0, deadline - time.monotonic())) and box:
                    result[channel] = box[0]
                    del self._inflight[channel]
                else:
                    result[channel] = unavailable(channel, self.clock(), reason="PROVIDER_FAILED")
            return result


class IntelligenceMarketFeed:
    """Compose with the existing minute worker; provider failure preserves 1m exits."""
    def __init__(self, market_feed, intelligence_feed):
        self.market_feed, self.intelligence_feed = market_feed, intelligence_feed

    def fetch(self, now):
        frames, errors = self.market_feed.fetch(now)
        declared = getattr(self.market_feed, "data_mode", "UNAVAILABLE")
        supplied = frames.get(MARKET_KEY, {})
        supplied = supplied if isinstance(supplied, dict) else {"data_mode": "UNAVAILABLE"}
        mode = declared
        if declared in ("FIXTURE", "TEST_DATA") or supplied.get("data_mode") in ("FIXTURE", "TEST_DATA") or supplied.get("is_synthetic") is True:
            mode = "FIXTURE"
        elif declared not in ("LIVE", "DELAYED") or ("data_mode" in supplied and supplied["data_mode"] not in ("LIVE", "DELAYED")):
            mode = "UNAVAILABLE"
        elif supplied.get("data_mode") == "DELAYED":
            mode = "DELAYED"
        if "is_synthetic" in supplied and type(supplied["is_synthetic"]) is not bool:
            mode = "UNAVAILABLE"
        market = {"provider": getattr(self.market_feed, "name", "unidentified-provider"),
                  "data_mode": mode,
                  "retrieved_at": stamp(self.intelligence_feed.clock())}
        return dict(frames, **{MARKET_KEY: market, BUNDLE_KEY: self.intelligence_feed.fetch(now)}), errors
