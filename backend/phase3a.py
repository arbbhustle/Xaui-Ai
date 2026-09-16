"""Demo-only macro/news overlay, using the committed Phase 2 execution engine."""
from dataclasses import asdict
from pathlib import Path
import json

from .council import CouncilPolicy, evaluate_council
from .domain import Policy, apply_veto, digest, parse
from .phase2 import Phase2Engine, SOURCE_FILES as PHASE2_SOURCES
from .intelligence import IntelligencePolicy, assess_intelligence
from .intelligence_providers import BUNDLE_KEY, MARKET_KEY, normalize_bundle, text, timestamp
from .intelligence_journal import remember_news
from .storage import Store

VERSION = "phase3a-1"
MODEL_VERSION = "macro-news-council-v1"
SOURCE_FILES = (*PHASE2_SOURCES, "intelligence_providers.py", "intelligence.py", "intelligence_journal.py", "phase3a.py")
BLEND = {"technical": .70, "usd_score": .10, "yields_score": .08, "macro_score": .05, "news_sentiment_score": .07}


def implementation_hash(root=None):
    root = Path(root) if root is not None else Path(__file__).parent
    return digest({name: (root / name).read_text(encoding="utf-8") for name in SOURCE_FILES})


def assess_snapshot(snapshot, now, intelligence_policy):
    intelligence = assess_intelligence(snapshot["intelligence"], now, intelligence_policy)
    market = snapshot["market_provenance"]
    intelligence["market_provenance"] = market
    if market["data_mode"] == "UNAVAILABLE":
        intelligence["vetoes"].append("UNVERIFIED_XAU_PROVENANCE")
        if intelligence["data_mode"] != "FIXTURE":
            intelligence["data_mode"] = "UNAVAILABLE"
    if market["data_mode"] == "FIXTURE":
        intelligence["data_mode"] = "FIXTURE"
        if not intelligence_policy.allow_fixture_data:
            intelligence["vetoes"].append("FIXTURE_DATA_DISABLED")
    elif market["data_mode"] == "DELAYED" and intelligence["data_mode"] == "LIVE":
        intelligence["data_mode"] = "DELAYED"
    intelligence["vetoes"] = sorted(set(intelligence["vetoes"]))
    intelligence["vetoes"] += snapshot.get("provenance_vetoes", [])
    intelligence["status"] = "BLOCKED" if intelligence["vetoes"] else "READY"
    return intelligence


def evaluate_phase3a(snapshot, policy):
    intelligence = assess_snapshot(snapshot, parse(snapshot["observed_at"]), IntelligencePolicy(**snapshot["intelligence_policy"]))

    def overlay(technical):
        original = {"buy_score": technical["buy_score"], "sell_score": technical["sell_score"]}
        extra = {"technical_scores": original, "combined_scores": None, "blend_weights": {}}
        scores = intelligence["scores"]
        if any(scores[k] is None for k in ("usd_score", "yields_score", "news_sentiment_score")):
            return extra
        weights = {k: v for k, v in BLEND.items() if k == "technical" or scores[k] is not None}
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}
        for side, sign in (("buy", 1), ("sell", -1)):
            extra[f"{side}_score"] = round(weights["technical"] * original[f"{side}_score"] + sum(
                weight * (50 + sign * scores[k] / 2) for k, weight in weights.items() if k != "technical"), 4)
        extra["combined_scores"] = {k: extra[k] for k in ("buy_score", "sell_score")}
        extra["blend_weights"] = weights
        return extra

    result = evaluate_council(snapshot, policy, score_overlay=overlay)
    result.setdefault("technical_scores", {"buy_score": result["buy_score"], "sell_score": result["sell_score"]})
    result.setdefault("combined_scores", None)
    result.setdefault("blend_weights", {})
    result.update(strategy_version=VERSION, model_version=MODEL_VERSION,
                  engine="DardaniaXAUTRADE AI Phase 3A", intelligence=intelligence,
                  intelligence_components=intelligence["scores"])
    result["market_provenance"] = snapshot["market_provenance"]
    result["reasons"] += intelligence["reasons"]
    result = apply_veto(result, intelligence["vetoes"])
    if intelligence["vetoes"]:
        result["data_status"] = "INTELLIGENCE_BLOCKED"
    elif result["data_status"] == "LIVE_DATA":
        result["data_status"] = {"FIXTURE": "FIXTURE_DATA", "DELAYED": "DELAYED_DATA", "LIVE": "LIVE_DATA"}[intelligence["data_mode"]]
    return result


class Phase3AEngine(Phase2Engine):
    strategy_version = VERSION
    replay_fields = Phase2Engine.replay_fields + ("intelligence", "intelligence_components", "technical_scores", "combined_scores", "blend_weights", "market_provenance")

    def __init__(self, store, policy=Policy(), council_policy=CouncilPolicy(), intelligence_policy=IntelligencePolicy()):
        super().__init__(store, policy, council_policy)
        self.intelligence_policy = intelligence_policy
        self.code_hash = implementation_hash()
        self.model_identity = digest({"code_hash": self.code_hash, "model_version": MODEL_VERSION,
                                      "policy": asdict(policy), "council_policy": asdict(council_policy),
                                      "intelligence_policy": asdict(intelligence_policy)})

    def snapshot_context(self, conn, snapshot, checked):
        raw = snapshot["frames"].pop(MARKET_KEY, {})
        try:
            market = {"provider": text(raw["provider"]), "data_mode": raw["data_mode"],
                      "retrieved_at": timestamp(raw["retrieved_at"])}
            if market["data_mode"] not in ("LIVE", "DELAYED", "FIXTURE") or parse(market["retrieved_at"]) > parse(snapshot["observed_at"]):
                raise ValueError("Invalid XAU provenance")
        except (KeyError, TypeError, ValueError, AttributeError):
            market = {"provider": "unverified", "data_mode": "UNAVAILABLE", "retrieved_at": None}
        snapshot["market_provenance"] = market
        bundle = normalize_bundle(snapshot["frames"].pop(BUNDLE_KEY, {}), parse(snapshot["observed_at"]))
        remember_news(conn, bundle["news"], parse(snapshot["observed_at"]))
        snapshot["intelligence"] = bundle
        snapshot["intelligence_policy"] = asdict(self.intelligence_policy)
        # Fixture/delayed/live outcomes must never train one another's confidence.
        modes = {k: v["data_mode"] for k, v in bundle.items()}
        modes["xau"] = market["data_mode"]
        category = "FIXTURE" if "FIXTURE" in modes.values() else "REAL"
        bound = Store.get_state(conn, "phase3a_input_category", None)
        if bound is None and market["data_mode"] != "UNAVAILABLE" and all(bundle[k]["status"] == "OK" for k in ("usd", "yields", "calendar", "news")):
            Store.set_state(conn, "phase3a_input_category", category)
            bound = category
        snapshot["provenance_vetoes"] = ["INPUT_MODE_DATABASE_MISMATCH"] if bound and bound != category and market["data_mode"] != "UNAVAILABLE" else []
        identity = {"model": self.model_identity, "input_modes": modes}
        if "model_namespace_extra" in snapshot:
            identity["extra"] = snapshot["model_namespace_extra"]
        snapshot["model_identity"] = digest(identity)
        return super().snapshot_context(conn, snapshot, checked)

    def evaluate_snapshot(self, snapshot, policy):
        return evaluate_phase3a(snapshot, policy)

    def monitor_gates(self, trade, snapshot):
        old = trade.get("market_provenance", {}).get("data_mode", "UNAVAILABLE")
        current = snapshot["market_provenance"]["data_mode"]
        if current == "UNAVAILABLE" or (old == "FIXTURE") != (current == "FIXTURE"):
            return ["XAU_MONITOR_PROVENANCE_MISMATCH"]
        return []

    def seal_decision(self, result):
        return dict(result, decision_checksum=digest(result))

    def replay(self, decision_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT payload FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if not row:
            raise KeyError(decision_id)
        decision = json.loads(row[0])
        checksum = decision.pop("decision_checksum", None)
        if checksum != digest(decision):
            raise ValueError("Decision checksum mismatch")
        return super().replay(decision_id)

    def trade_metadata(self, result):
        return {**super().trade_metadata(result), **{k: result[k] for k in
                ("intelligence", "intelligence_components", "technical_scores", "combined_scores", "blend_weights", "market_provenance")}}

    def _response_intelligence(self, conn, snapshot_id, now):
        row = conn.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            return {"status": "BLOCKED", "vetoes": ["MISSING_INTELLIGENCE_SNAPSHOT"]}
        snapshot = json.loads(row[0])
        policy = dict(snapshot["intelligence_policy"])
        policy["allow_fixture_data"] &= self.intelligence_policy.allow_fixture_data
        return assess_snapshot(snapshot, now, IntelligencePolicy(**policy))

    def api_health(self, conn, health, now):
        view = self._response_intelligence(conn, health.get("snapshot_id"), now)
        health["intelligence"] = view
        if view["vetoes"]:
            health["status"] = "DEGRADED"
            health["data_errors"] = sorted(set(health["data_errors"] + view["vetoes"]))
        return health

    def api_decision(self, conn, result, now):
        view = self._response_intelligence(conn, result.get("snapshot_id"), now)
        result["intelligence_at_response"] = view
        if view["vetoes"]:
            result = apply_veto(result, view["vetoes"])
            result["data_status"] = "INTELLIGENCE_BLOCKED"
        return result
