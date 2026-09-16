"""Deterministic empirical confidence for closed, gross demo outcomes only."""
from .domain import digest, parse

CALIBRATION_VERSION = "demo-beta-bins-v1"


def bucket(score):
    return min(4, max(0, int(score // 20)))


def calibrate(direction, score, samples, cutoff, minimum_samples=30):
    """Beta(2,2) smoothing by direction and score bin; no future-known labels."""
    selected = []
    seen = set()
    for sample in samples:
        if sample["trade_id"] in seen:
            raise ValueError("Duplicate calibration outcome")
        seen.add(sample["trade_id"])
        if (parse(sample["closed_at"]) <= cutoff and parse(sample["known_at"]) <= cutoff
                and sample["direction"] == direction
                and bucket(sample["raw_score"]) == bucket(score)):
            selected.append(sample)
    wins = sum(s["win"] for s in selected)
    count = len(selected)
    ready = count >= minimum_samples
    return {
        "version": CALIBRATION_VERSION, "status": "READY" if ready else "WARMUP",
        "target": "POSITIVE_R_CLOSED_GROSS_DEMO", "direction": direction,
        "score_bin": [bucket(score) * 20, min(100, (bucket(score) + 1) * 20)],
        "sample_count": count, "wins": wins, "minimum_samples": minimum_samples,
        "prior_alpha": 2, "prior_beta": 2,
        "probability": round((wins + 2) / (count + 4), 6) if ready else None,
        "training_digest": digest(selected),
    }
