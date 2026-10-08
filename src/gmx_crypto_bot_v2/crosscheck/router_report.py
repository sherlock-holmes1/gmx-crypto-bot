"""Deterministic evidence report for read-only GMX router checks."""

from __future__ import annotations

from collections import Counter
from typing import Any

from gmx_crypto_bot_v2.crosscheck.router_preflight import OracleInput, RouterPreflight

TARGET_CATEGORIES = ("long_increase", "short_increase", "long_decrease", "short_decrease")
REQUEST_FIELDS = ("account", "market", "orderType", "isLong", "initialCollateralToken",
                  "sizeDeltaUsd", "initialCollateralDeltaAmount", "acceptablePrice",
                  "triggerPrice", "swapPath", "isFrozen")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str) and a.startswith("0x") and b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


def compare(preflight: dict[str, Any], reconstruction: dict[str, Any] | None) -> dict[str, Any]:
    """Reject caller-supplied model claims until a registered adapter exists."""
    if preflight.get("outcome") not in {"passed_preflight", "validation_error"}:
        return {"status": "gmx_preflight_only", "reason": "no_decoded_gmx_decision"}
    return {"status": "gmx_preflight_only", "reason": "registered_execution_adapter_unavailable"}


def build_report(candidates: list[dict[str, Any]], preflight: RouterPreflight | None,
                 oracles: dict[str, OracleInput], reconstructions: dict[str, dict[str, Any]] | None = None,
                 *, source: str = "historical", recording: str = "", rule_adapter: Any = None,
                 decision_adapter: Any = None) -> dict[str, Any]:
    reconstructions = reconstructions or {}
    rows: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda c: (
            c.get("creation", {}).get("block_number", -1),
            c.get("creation", {}).get("transaction_index", -1),
            c.get("creation", {}).get("log_index", -1))):
        key = candidate.get("order_key")
        oracle = oracles.get(str(key).lower())
        if candidate.get("selection_skip_reasons"):
            call = {"order_key": key, "outcome": "ineligible_latest_request",
                    "reason": "selection_ineligible", "selection_skip_reasons": candidate["selection_skip_reasons"]}
        elif preflight is None:
            call = {"order_key": key, "outcome": "provider_failure", "reason": "archive_rpc_or_deployment_missing"}
        elif oracle is None:
            call = {"order_key": key, "outcome": "evidence_failure",
                    "reason": "watch_oracle_price_provenance_missing" if source == "watch"
                    else "verified_oracle_input_missing"}
            if candidate.get("oracle_evidence_failure"):
                call["reason"] = "watch_oracle_evidence_rejected"
                call["oracle_evidence_failure"] = candidate["oracle_evidence_failure"]
        else:
            call = preflight.run(candidate, oracle)
        comparison = (decision_adapter.evaluate(call) if decision_adapter is not None and
                      str(key).lower() == decision_adapter.oracle.get("order_key") else
                      compare(call, reconstructions.get(str(key).lower())))
        rule_result = (rule_adapter.evaluate(call) if rule_adapter is not None else
                       {"status": "unavailable", "reason": "pinned_economics_reader_unavailable"})
        rows.append({"source": source, "category": candidate.get("category"),
                     "order_key": key, "creation": candidate.get("creation"),
                     "oracle_evidence_failure": candidate.get("oracle_evidence_failure"),
                     "terminal": candidate.get("terminal"), "proposed_pin_block": candidate.get("proposed_pin_block"),
                     "preflight": call, "comparison": comparison, "rule_comparison": rule_result})
    coverage: dict[str, Any] = {}
    for category in TARGET_CATEGORIES:
        subset = [row for row in rows if row["category"] == category]
        coverage[category] = {"candidates": len(subset),
                              "preflight_outcomes": dict(sorted(Counter(r["preflight"]["outcome"] for r in subset).items())),
                              "comparison_outcomes": dict(sorted(Counter(r["comparison"]["status"] for r in subset).items())),
                              "gap": None if any(r["comparison"]["status"] in {"agreement", "disagreement"} for r in subset)
                              else "no_verified_same_order_comparison"}
    return {"schema_version": 1, "source": source, "recording": recording,
            "deployment": (vars(preflight.deployment) if preflight else None),
            "candidates": rows, "coverage": coverage,
            "completion_gate_met": any(r["comparison"]["status"] in {"agreement", "disagreement"} for r in rows)}
