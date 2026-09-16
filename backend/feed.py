"""Bounded Twelve Data reads. Never log URLs, API keys, or provider error bodies."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import httpx

from .domain import INTERVALS, parse, stamp


class TwelveDataFeed:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.cache = {}

    def _fetch(self, interval, now):
        cached = self.cache.get(interval)
        if cached and now < cached[0] and interval != "1min":
            return cached[1]
        if not self.api_key:
            raise RuntimeError("API_KEY_MISSING")
        with httpx.Client(timeout=httpx.Timeout(8), follow_redirects=False) as client:
            response = client.get("https://api.twelvedata.com/time_series", params={
                "symbol": "XAU/USD", "interval": interval, "timezone": "UTC",
                "outputsize": 1000 if interval == "1min" else 125,
                "apikey": self.api_key, "format": "JSON",
            })
            response.raise_for_status()
            payload = response.json()
        if payload.get("status") == "error" or not isinstance(payload.get("values"), list):
            raise RuntimeError("PROVIDER_REJECTED_REQUEST")
        rows = []
        for raw in payload["values"]:
            text = raw["datetime"].replace(" ", "T")
            # The request explicitly forces UTC; provider often omits the offset.
            if len(text) == 19:
                text += "+00:00"
            rows.append({"t": stamp(parse(text)), "o": float(raw["open"]),
                         "h": float(raw["high"]), "l": float(raw["low"]),
                         "c": float(raw["close"]), "v": None})
        rows.sort(key=lambda r: r["t"])
        if rows:
            # If the newest row is forming, fetch after it closes; otherwise retry
            # next minute until the provider publishes the next bar.
            until = parse(rows[-1]["t"]) + timedelta(seconds=INTERVALS[interval] + 10)
            self.cache[interval] = (until, rows)
        return rows

    def fetch(self, now):
        frames, errors = {}, {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            tasks = {k: pool.submit(self._fetch, k, now) for k in INTERVALS}
            for interval, task in tasks.items():
                try:
                    frames[interval] = task.result()
                except Exception as exc:
                    # Do not reuse a cached value after its refresh failed.
                    errors[interval] = type(exc).__name__
                    frames[interval] = []
        return frames, errors
