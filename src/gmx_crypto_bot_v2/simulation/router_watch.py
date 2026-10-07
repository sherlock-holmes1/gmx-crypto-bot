"""Bounded, read-only public OrderCreated watcher."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.domain.constants import DECREASE_ORDER_TYPES, INCREASE_ORDER_TYPES
from gmx_crypto_bot_v2.domain.entries import _event_entry_from_log, decoded_log


def watch_order_creations(rpc: Any, emitter: str, start_block: int, end_block: int,
                          target_market: str,
                          *, max_span: int = 7200, chunk_size: int = 500) -> list[dict[str, Any]]:
    if not 0 <= start_block <= end_block or end_block - start_block > max_span:
        raise ValueError("invalid bounded watch range")
    if not isinstance(emitter, str) or not emitter.startswith("0x") or len(emitter) != 42:
        raise ValueError("invalid EventEmitter address")
    if not isinstance(target_market, str) or not target_market.startswith("0x") or len(target_market) != 42:
        raise ValueError("invalid target market")
    candidates: list[dict[str, Any]] = []
    terminal_by_key: set[tuple[int, str]] = set()
    for first in range(start_block, end_block + 1, chunk_size):
        last = min(end_block, first + chunk_size - 1)
        envelope = rpc.request("eth_getLogs", [{"address": emitter, "fromBlock": hex(first),
                                               "toBlock": hex(last)}])
        if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), list):
            raise ValueError(f"eth_getLogs failed for {first}-{last}")
        for log in envelope["result"]:
            if not isinstance(log, dict) or log.get("address", "").lower() != emitter.lower():
                raise ValueError("watch log emitter mismatch")
            try:
                block_number = int(log["blockNumber"], 16)
                if block_number < first or block_number > last:
                    raise ValueError("watch log outside requested block range")
                decoded = decoded_log(log)
                if decoded.event_name in {"OrderExecuted", "OrderCancelled"}:
                    terminal_key = decoded.values.get("key")
                    if isinstance(terminal_key, str):
                        terminal_by_key.add((block_number, terminal_key.lower()))
                    continue
                if decoded.event_name != "OrderCreated":
                    continue
                entry = _event_entry_from_log(log, decoded)
                values = entry["values"]
                if not isinstance(values.get("market"), str) or values["market"].lower() != target_market.lower():
                    continue
                order_type = values.get("orderType")
                action = ("increase" if order_type in INCREASE_ORDER_TYPES else
                          "decrease" if order_type in DECREASE_ORDER_TYPES and order_type != 7 else None)
                side = "long" if values.get("isLong") is True else "short" if values.get("isLong") is False else None
                key = values.get("key")
                reasons = []
                if action is None:
                    reasons.append("unsupported_order_type")
                if not isinstance(key, str):
                    reasons.append("missing_order_key")
                if side is None:
                    reasons.append("invalid_side")
                candidates.append({"source": "watch", "order_key": key.lower() if isinstance(key, str) else None,
                                   "order_type": order_type, "action": action, "side": side,
                                   "category": f"{side}_{action}" if side and action else None,
                                   "creation": {field: entry.get(field) for field in (
                                       "event_name", "block_number", "transaction_index", "log_index",
                                       "transaction_hash", "block_hash")},
                                   "proposed_pin_block": entry["block_number"],
                                   "proposed_pin_hash": entry["block_hash"],
                                   "creation_request": values, "request_at_proposed_pin": values,
                                   "selection_skip_reasons": reasons, "terminal": None,
                                   "oracle_evidence": "unavailable_until_verified_oracle_input_supplied"})
            except (KeyError, TypeError, ValueError) as failure:
                raise ValueError(f"malformed watch log at block {first}-{last}: {failure}") from failure
    for candidate in candidates:
        if (candidate["proposed_pin_block"], candidate["order_key"]) in terminal_by_key:
            candidate["selection_skip_reasons"].append("same_block_terminal")
    return sorted(candidates, key=lambda row: (row["creation"]["block_number"],
                                                row["creation"]["transaction_index"],
                                                row["creation"]["log_index"]))
