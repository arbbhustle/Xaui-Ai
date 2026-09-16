"""Point-in-time macro/news scoring. All inputs and rules are replayable."""
from dataclasses import dataclass, asdict
from datetime import timedelta
from collections import defaultdict
import re

from .domain import digest, parse, stamp
from .intelligence_providers import CHANNELS, CRITICAL_EVENTS

EVENT_WINDOWS = {"CPI": (60, 30), "NFP": (45, 30), "PCE": (30, 20), "FOMC": (90, 60), "OTHER": (30, 15)}
IMPACT = {"HIGH": 1.0, "MEDIUM": .5, "LOW": .2}
RELEVANCE = re.compile(r"\b(gold|xau|usd|dollar|fed|rates?|treasur\w*|yields?|inflation|cpi|nfp|pce|fomc|war|conflict|ceasefire|airstrike|geopolit\w*)\b")
POSITIVE = ("gold rises", "gold rallies", "dollar falls", "dollar weakens", "yields fall", "yields decline",
            "rate cut", "dovish", "conflict escalates", "airstrike")
NEGATIVE = ("gold falls", "gold drops", "dollar rises", "dollar strengthens", "yields rise", "yields climb",
            "rate hike", "hawkish", "ceasefire")


@dataclass(frozen=True)
class IntelligencePolicy:
    usd_max_age: int = 300
    yields_max_age: int = 900
    macro_max_age: int = 86400
    calendar_max_age: int = 21600
    news_max_age: int = 900
    momentum_seconds: int = 3600
    calendar_lookahead_hours: int = 24
    news_half_life_seconds: int = 3600
    news_horizon_seconds: int = 21600
    allow_fixture_data: bool = False

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == "allow_fixture_data":
                if type(value) is not bool:
                    raise ValueError("Invalid fixture policy")
            elif type(value) is not int or not 1 <= value <= 604800:
                raise ValueError("Invalid intelligence policy")
        if self.calendar_lookahead_hours < 24 or self.calendar_lookahead_hours > 168:
            raise ValueError("Calendar coverage must span 24..168 hours")


def clip(value):
    return max(-1.0, min(1.0, value))


def conflict(items):
    positive = {a["source"] for a in items if a["gold_impact"] >= .6}
    negative = {a["source"] for a in items if a["gold_impact"] <= -.6}
    return bool(positive and negative and len(positive | negative) > 1)


def series_score(records, channel, now, policy):
    groups = defaultdict(list)
    for row in records:
        groups[(row["source"], row["instrument"])].append(row)
    analysis, vetoes = [], []
    for (source, instrument), group in sorted(groups.items()):
        by_time = {}
        for row in group:
            key = row["observed_at"]
            old = by_time.get(key)
            if old and old["published_at"] == row["published_at"] and old["value"] != row["value"]:
                vetoes.append(f"CONFLICTING_HIGH_IMPACT_SOURCES:{channel}")
            if not old or row["published_at"] > old["published_at"]:
                by_time[key] = row
        ordered = sorted(by_time.values(), key=lambda x: x["observed_at"])
        latest = ordered[-1]
        age = (now - parse(latest["observed_at"])).total_seconds()
        if age >= getattr(policy, f"{channel}_max_age"):
            vetoes.append(f"STALE_INTELLIGENCE:{channel}:{instrument}")
            continue
        target = parse(latest["observed_at"]) - timedelta(seconds=policy.momentum_seconds)
        anchors = [r for r in ordered if parse(r["observed_at"]) <= target]
        if not anchors or (target - parse(anchors[-1]["observed_at"])).total_seconds() > policy.momentum_seconds * .25:
            vetoes.append(f"MISSING_MOMENTUM_HISTORY:{instrument}")
            continue
        first = anchors[-1]
        change = ((latest["value"] / first["value"] - 1) * 100 if channel == "usd"
                  else (latest["value"] - first["value"]) * 100)
        impact = -clip(change / (.3 if channel == "usd" else 5))
        analysis.append({"source": source, "instrument": instrument, "value": latest["value"],
                         "direction": "UP" if change > 1e-9 else "DOWN" if change < -1e-9 else "FLAT",
                         "momentum": round(change, 6), "unit": "PERCENT_CHANGE" if channel == "usd" else "BASIS_POINTS",
                         "gold_impact": round(impact, 6), "anchor_at": first["observed_at"],
                         "source_at": latest["observed_at"], "published_at": latest["published_at"],
                         "url": latest["url"], "age_seconds": age})
    required = {"US2Y", "US10Y"} if channel == "yields" else set()
    if not analysis or not required.issubset({r["instrument"] for r in analysis}):
        vetoes.append(f"MISSING_CRITICAL_INTELLIGENCE:{channel}")
    if conflict(analysis):
        vetoes.append(f"CONFLICTING_HIGH_IMPACT_SOURCES:{channel}")
    # Average by instrument first, so adding providers cannot overweight one tenor.
    values = defaultdict(list)
    for row in analysis:
        values[row["instrument"]].append(row["gold_impact"])
    score = sum(sum(v) / len(v) for v in values.values()) / len(values) if values else None
    return score, analysis, vetoes


def macro_score(records, now, policy):
    eligible = [r for r in records if now < parse(r["meeting_at"]) <= now + timedelta(days=365)]
    if not eligible:
        return None, [], []
    nearest = min(r["meeting_at"] for r in eligible)
    per_source, vetoes = {}, []
    for row in eligible:
        if row["meeting_at"] == nearest:
            old = per_source.get(row["source"])
            if old and old["published_at"] == row["published_at"] and any(
                    old[k] != row[k] for k in ("expected_rate", "target_lower", "target_upper")):
                vetoes.append("CONFLICTING_HIGH_IMPACT_SOURCES:macro")
            if row["source"] not in per_source or row["published_at"] > per_source[row["source"]]["published_at"]:
                per_source[row["source"]] = row
    analysis = []
    for source, row in sorted(per_source.items()):
        if (now - parse(row["published_at"])).total_seconds() >= policy.macro_max_age:
            vetoes.append("STALE_INTELLIGENCE:macro:expectations")
            continue
        delta = (row["expected_rate"] - (row["target_lower"] + row["target_upper"]) / 2) * 100
        analysis.append(dict(row, expected_change_bps=round(delta, 6), gold_impact=round(-clip(delta / 25), 6)))
    if conflict(analysis):
        vetoes.append("CONFLICTING_HIGH_IMPACT_SOURCES:macro")
    return (sum(r["gold_impact"] for r in analysis) / len(analysis) if analysis else None), analysis, vetoes


def calendar_score(envelope, now, policy):
    coverage = envelope.get("coverage", {})
    if (not coverage.get("complete") or parse(coverage["start"]) > now - timedelta(hours=6)
            or parse(coverage["end"]) < now + timedelta(hours=policy.calendar_lookahead_hours)):
        return None, [], ["MISSING_CRITICAL_EVENT_DATA"]
    # Only the latest known revision from each source counts; other sources may disagree.
    latest, vetoes = {}, []
    for row in envelope["records"]:
        key = (row["source"], row["event_key"])
        old = latest.get(key)
        if old and any(parse(r["end_at"]) >= now - timedelta(hours=6) for r in (old, row)) and old["published_at"] == row["published_at"] and any(
                old[k] != row[k] for k in ("scheduled_at", "end_at", "status", "kind", "importance")):
            vetoes.append("CONFLICTING_HIGH_IMPACT_SOURCES:calendar")
        if key not in latest or row["published_at"] > latest[key]["published_at"]:
            latest[key] = row
    groups = defaultdict(list)
    for row in latest.values():
        groups[row["event_key"]].append(row)
    risk, events = 0.0, []
    for key, rows in sorted(groups.items()):
        if not any(parse(r["end_at"]) >= now - timedelta(hours=6) and
                   parse(r["scheduled_at"]) <= now + timedelta(hours=policy.calendar_lookahead_hours) for r in rows):
            continue
        if len({(r["scheduled_at"], r["end_at"], r["status"], r["kind"], r["importance"]) for r in rows}) > 1:
            vetoes.append("CONFLICTING_HIGH_IMPACT_SOURCES:calendar")
        for row in rows:
            start, end = parse(row["scheduled_at"]), parse(row["end_at"])
            if (row["status"] == "CANCELLED" or start > now + timedelta(hours=policy.calendar_lookahead_hours)
                    or end < now - timedelta(hours=6)):
                continue
            pre, post = EVENT_WINDOWS[row["kind"]]
            critical = row["kind"] in CRITICAL_EVENTS or row["importance"] == "HIGH"
            until = (start - now).total_seconds()
            blocked = start - timedelta(minutes=pre) <= now <= end + timedelta(minutes=post)
            phase = "PRE_EVENT" if now < start else "DURING_EVENT" if now <= end else "POST_EVENT"
            weight = 1 if row["kind"] in CRITICAL_EVENTS else IMPACT[row["importance"]]
            event_risk = 100 * weight if blocked else max(0, 50 * weight * (1 - max(0, until) / 86400)) if until > 0 else 0
            risk = max(risk, event_risk)
            if critical and blocked:
                vetoes.append("MAJOR_EVENT_IMMINENT" if phase == "PRE_EVENT" else "MAJOR_EVENT_BLACKOUT")
            events.append(dict(row, seconds_until=until, phase=phase, blackout=blocked and critical,
                               blackout_start=stamp(start - timedelta(minutes=pre)),
                               blackout_end=stamp(end + timedelta(minutes=post)), risk=round(event_risk, 4)))
    return round(risk, 4), events, vetoes


def classify_headline(row, age, policy):
    title = row["title"].lower()
    relevance = min(1.0, len(RELEVANCE.findall(title)) / 2)
    positive, negative = [p for p in POSITIVE if p in title], [p for p in NEGATIVE if p in title]
    uncertain = bool(re.search(r"\b(not|denies|denied|rumou?r|unconfirmed)\b", title))
    sentiment = 0 if uncertain or bool(positive) == bool(negative) else .8 if positive else -.8
    decay = 2 ** (-age / policy.news_half_life_seconds) if age <= policy.news_horizon_seconds else 0
    return {"relevance": relevance, "sentiment": sentiment, "importance": row["importance"],
            "age_seconds": age, "decay": round(decay, 6), "method": "RULE_LEXICON_V1",
            "matched_phrases": positive + negative, "uncertain": uncertain,
            "gold_impact": round(sentiment * relevance * decay, 6)}


def news_score(records, now, policy):
    # Dedup by publisher ID, canonical URL or normalized headline, including syndication.
    groups, keys_to_group = [], {}
    for row in sorted(records, key=lambda r: (r["published_at"], r["source"], r["id"]), reverse=True):
        headline = " ".join(re.findall(r"\w+", row["title"].lower()))
        keys = {"id:" + row["source"] + ":" + row["id"], "url:" + row["url"], "title:" + headline}
        found = {keys_to_group[k] for k in keys if k in keys_to_group}
        if found:
            index = min(found)
            # Merge clusters when a third syndicated item links two identities.
            for other in found - {index}:
                groups[index].extend(groups[other])
                groups[other] = []
                for key in list(keys_to_group):
                    if keys_to_group[key] == other:
                        keys_to_group[key] = index
            groups[index].append(row)
        else:
            index = len(groups)
            groups.append([row])
        for key in keys:
            keys_to_group[key] = index
    items, ages = [], {}
    for group in groups:
        if not group:
            continue
        newest = max(group, key=lambda r: (r["published_at"], r["source"], r["id"]))
        oldest = min(parse(r.get("first_published_at", r["published_at"])) for r in group)
        age = (now - oldest).total_seconds()  # syndication cannot rejuvenate an old story
        for row in group:
            ages[(row["source"], row["id"], row["published_at"])] = age
        items.append({"story_id": digest(sorted({r["url"] for r in group})), "title": newest["title"],
                      "source": newest["source"], "sources": group, "duplicates_removed": len(group) - 1,
                      **classify_headline(newest, age, policy)})
    # Deduplication must not erase opposing high-impact reports about one story.
    editions, vetoes = {}, []
    for row in records:
        key = (row["source"], row["id"])
        old = editions.get(key)
        if old and old["published_at"] == row["published_at"] and any(old[k] != row[k] for k in ("title", "importance")):
            vetoes.append("CONFLICTING_NEWS_REVISION")
        if key not in editions or row["published_at"] > editions[key]["published_at"]:
            editions[key] = row
    high = [dict(source=r["source"], **classify_headline(r, ages[(r["source"], r["id"], r["published_at"])], policy))
            for r in editions.values() if r["importance"] == "HIGH"]
    if conflict(high):
        vetoes.append("CONFLICTING_HIGH_IMPACT_SOURCES:news")
    denom = sum(IMPACT[item["importance"]] * item["relevance"] for item in items
                if item["age_seconds"] <= policy.news_horizon_seconds)
    score = sum(item["gold_impact"] * IMPACT[item["importance"]] for item in items) / max(denom, 1)
    return round(score, 6), sorted(items, key=lambda r: r["story_id"]), vetoes


def assess_intelligence(bundle, now, policy):
    """As-of evaluation; future schedules are allowed, future knowledge is not."""
    scores = {"usd_score": None, "yields_score": None, "macro_score": None,
              "news_sentiment_score": None, "event_risk_score": None}
    result = {"as_of": stamp(now), "scores": scores, "sources": [], "details": {}, "vetoes": [], "reasons": []}
    vetoes, usable = [], {}
    modes = {e["data_mode"] for e in bundle.values() if e["status"] == "OK"}
    result["data_mode"] = "FIXTURE" if "FIXTURE" in modes else "DELAYED" if "DELAYED" in modes else "LIVE" if modes else "UNAVAILABLE"
    if "FIXTURE" in modes and not policy.allow_fixture_data:
        vetoes.append("FIXTURE_DATA_DISABLED")
    for channel in CHANNELS:
        envelope = bundle[channel]
        audit = {"channel": channel, "provider": envelope["provider"], "data_mode": envelope["data_mode"],
                 "source_timestamp": envelope["as_of"], "retrieved_at": envelope["retrieved_at"],
                 "fresh": False, "age_seconds": None,
                 "records": [{k: r[k] for k in ("id", "source", "url", "published_at")} for r in envelope["records"]]}
        result["sources"].append(audit)
        if envelope["status"] != "OK":
            result["reasons"].append(f"{channel}: provider unavailable")
            if channel != "macro":
                vetoes.append("MISSING_CRITICAL_EVENT_DATA" if channel == "calendar" else f"MISSING_CRITICAL_INTELLIGENCE:{channel}")
            elif (envelope["provider"] != "not-configured"
                  or envelope.get("error") in ("PROVIDER_FAILED", "INVALID_OR_MISSING_PROVIDER_DATA")):
                vetoes.append("MACRO_PROVIDER_FAILURE")
            continue
        published = parse(envelope["as_of"])
        received = parse(envelope["retrieved_at"])
        age = (now - published).total_seconds()
        audit["age_seconds"] = age
        future = published > received or received > now
        for row in envelope["records"]:
            future |= parse(row["published_at"]) > published
            if "observed_at" in row:
                future |= parse(row["observed_at"]) > parse(row["published_at"])
        if future:
            vetoes.append(f"FUTURE_INTELLIGENCE:{channel}")
        elif age >= getattr(policy, f"{channel}_max_age"):
            vetoes.append(f"STALE_INTELLIGENCE:{channel}")
        else:
            audit["fresh"] = True
            usable[channel] = envelope
    for channel, envelope in usable.items():
        if channel in ("usd", "yields"):
            score, details, problems = series_score(envelope["records"], channel, now, policy)
        elif channel == "macro":
            score, details, problems = macro_score(envelope["records"], now, policy)
        elif channel == "calendar":
            score, details, problems = calendar_score(envelope, now, policy)
        else:
            score, details, problems = news_score(envelope["records"], now, policy)
        key = {"calendar": "event_risk_score", "news": "news_sentiment_score"}.get(channel, channel + "_score")
        scores[key] = round(score * (1 if channel == "calendar" else 100), 4) if score is not None else None
        result["details"][channel] = details
        audit = next(item for item in result["sources"] if item["channel"] == channel)
        audit["validation_issues"] = sorted(set(problems))
        audit["feed_fresh"] = audit["fresh"]
        if any(p.startswith(("STALE_", "MISSING_")) for p in problems):
            audit["fresh"] = False
        vetoes.extend(problems)
        result["reasons"].append(f"{key}: {scores[key]}" if score is not None else f"{channel}: no usable observations")
    if scores["macro_score"] is None:
        result["reasons"].append("Rate expectations unavailable; no macro value inferred")
    result["vetoes"] = sorted(set(vetoes))
    if result["data_mode"] == "LIVE" and any(not a["fresh"] for a in result["sources"] if a["channel"] != "macro" or a["provider"] != "not-configured"):
        result["data_mode"] = "UNAVAILABLE"
    result["status"] = "BLOCKED" if vetoes else "READY"
    return result
