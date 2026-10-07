"""Select recorded orders for a later, read-only GMX router preflight.

This module makes no archive or router calls. Its gates are evidence requirements,
not claims that a recorded request is pending, latest, or state equivalent.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.constants import DECREASE_ORDER_TYPES, INCREASE_ORDER_TYPES
from gmx_crypto_bot_v2.domain.entries import _coordinate, _decode_recorded_log, _event_entry
from gmx_crypto_bot_v2.evidence.repository import event_rows


_NAMES = {
    "OrderCreated", "OrderUpdated", "OrderSizeDeltaAutoUpdated",
    "OrderCollateralDeltaAmountAutoUpdated", "OrderExecuted", "OrderCancelled",
    "OrderFrozen",
}
_TERMINAL = {"OrderExecuted", "OrderCancelled"}
_REQUIRED = ("key", "market", "orderType", "isLong", "account", "initialCollateralToken",
             "sizeDeltaUsd", "acceptablePrice", "triggerPrice")


def select_router_candidates(recording: Path) -> list[dict[str, Any]]:
    """Return creation-ordered candidates with explicit, unresolved evidence gates.

    The proposed pin is the end of the creation block. A same-block terminal
    event leaves no block boundary at which the order can be queried pending.
    """
    created: list[dict[str, Any]] = []
    lifecycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lifecycle_gaps: dict[str, list[tuple[tuple[int, int, int], str]]] = defaultdict(list)
    global_gaps: list[tuple[tuple[int, int, int], str]] = []
    for row in event_rows(recording, kinds={"gmx_market_log"}, names=_NAMES):
        declared_name = row["payload"].get("event_name")
        decoded = _decode_recorded_log(row)
        if decoded is None or decoded.event_name != declared_name:
            if declared_name == "OrderCreated":
                created.append(_undecodable(row))
            else:
                reason = ("lifecycle_log_decode_failed" if decoded is None
                          else "lifecycle_event_name_mismatch")
                key = decoded.values.get("key") if decoded is not None else None
                (lifecycle_gaps[key.lower()] if isinstance(key, str)
                 else global_gaps).append((_coordinate(row), reason))
            continue
        entry = _event_entry(row, decoded)
        key = decoded.values.get("key")
        if declared_name == "OrderCreated":
            created.append(entry)
        elif isinstance(key, str):
            lifecycle[key.lower()].append(entry)
        else:
            global_gaps.append((_coordinate(row), "lifecycle_key_missing"))

    result: list[dict[str, Any]] = []
    for creation in sorted(created, key=_coordinate):
        values = creation["values"]
        key = values.get("key")
        key = key.lower() if isinstance(key, str) else None
        reasons = list(creation.get("decode_errors", []))
        pin_end = (creation["block_number"], float("inf"), float("inf"))
        creation_coordinate = _coordinate(creation)
        reasons.extend(reason for coordinate, reason in
                       global_gaps + lifecycle_gaps.get(key, [])
                       if creation_coordinate <= coordinate <= pin_end)
        for field in _REQUIRED:
            if field not in values or values[field] is None:
                reasons.append(f"missing_creation_field:{field}")
        order_type = values.get("orderType")
        if order_type in INCREASE_ORDER_TYPES:
            action = "increase"
        elif order_type in DECREASE_ORDER_TYPES and order_type != 7:
            action = "decrease"
        else:
            action = None
            reasons.append("unsupported_order_type")
        is_long = values.get("isLong")
        side = "long" if is_long is True else "short" if is_long is False else None
        if side is None and "missing_creation_field:isLong" not in reasons:
            reasons.append("invalid_side")
        events = sorted(lifecycle.get(key, []), key=_coordinate) if key else []
        terminal = next((event for event in events if event["event_name"] in _TERMINAL), None)
        if terminal and terminal["block_number"] == creation["block_number"]:
            reasons.append("same_block_terminal")
        # A frozen order is retained for the archive pending-state check.
        updates = [event for event in events if event["event_name"] in {
                       "OrderUpdated", "OrderSizeDeltaAutoUpdated",
                       "OrderCollateralDeltaAmountAutoUpdated"}
                   and event["block_number"] == creation["block_number"]
                   and _coordinate(event) > _coordinate(creation)]
        request = dict(values)
        for update in updates:
            if update["event_name"] == "OrderSizeDeltaAutoUpdated":
                next_size = update["values"].get("nextSizeDeltaUsd")
                if next_size is None:
                    reasons.append("missing_auto_update_field:nextSizeDeltaUsd")
                else:
                    request["sizeDeltaUsd"] = next_size
            elif update["event_name"] == "OrderCollateralDeltaAmountAutoUpdated":
                next_collateral = update["values"].get("nextCollateralDeltaAmount")
                if next_collateral is None:
                    reasons.append("missing_auto_update_field:nextCollateralDeltaAmount")
                else:
                    request["initialCollateralDeltaAmount"] = next_collateral
            else:
                request.update(update["values"])
        result.append({
            "source": "recording",
            "recording": str(recording),
            "order_key": key,
            "order_type": order_type,
            "action": action,
            "side": side,
            "category": f"{side}_{action}" if side and action else None,
            "creation": _identity(creation),
            "proposed_pin_block": creation["block_number"],
            "proposed_pin_hash": creation.get("block_hash"),
            "creation_request": dict(values),
            "request_at_proposed_pin": request,
            "same_block_updates": [_identity(update) for update in updates],
            "terminal": _identity(terminal) if terminal else None,
            "frozen_events": [_identity(event) for event in events
                              if event["event_name"] == "OrderFrozen"],
            "selection_skip_reasons": sorted(set(reasons)),
            "router_eligibility_gate": "not_checked" if not reasons else "selection_ineligible",
            "same_order_state_gate": "not_checked" if not reasons else "selection_ineligible",
        })
    return result


def _identity(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: entry.get(field) for field in (
        "event_name", "block_number", "transaction_index", "log_index",
        "transaction_hash", "block_hash"
    )}


def _undecodable(row: dict[str, Any]) -> dict[str, Any]:
    log = row.get("payload", {}).get("log", {})
    return {
        "event_name": "OrderCreated",
        "values": {},
        "block_number": row["block_number"],
        "transaction_index": row["transaction_index"],
        "log_index": row["log_index"],
        "transaction_hash": log.get("transactionHash"),
        "block_hash": log.get("blockHash"),
        "decode_errors": ["creation_log_decode_failed"],
    }
