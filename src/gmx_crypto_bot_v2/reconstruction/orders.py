"""Join indexed requests, lifecycle events, receipt transfers and swaps."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.checkpoint import normalize_recorded_order_checkpoint
from gmx_crypto_bot_v2.domain.constants import (
    ARBITRUM_ORDER_VAULT,
    ERC20_TRANSFER_TOPIC,
    LIFECYCLE_EVENTS,
    POSITION_EVENTS,
    TERMINAL_EVENTS,
)
from gmx_crypto_bot_v2.domain.entries import (
    _coordinate,
    _decode_recorded_log,
    _event_entry,
    _event_entry_from_log,
    _order_key,
    decoded_log,
)
from gmx_crypto_bot_v2.domain.events import (
    EventDecodeError,
    event_name_from_data,
)
from gmx_crypto_bot_v2.evidence.repository import event_rows, raw_logs, receipt_rows
from gmx_crypto_bot_v2.reconstruction.positions import (
    _attach_pre_impact_pool,
    _attach_pre_position_state,
)
from gmx_crypto_bot_v2.reconstruction.swaps import STATE_EVENTS, SwapReplay


def _load_replay_evidence(
    recording: Path, target_market: str, index_token: str
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    int,
]:
    created: dict[str, dict[str, Any]] = {}
    opening: dict[str, dict[str, Any]] = {}
    observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    receipts: dict[str, dict[str, Any]] = {}
    opening_positions: dict[str, dict[str, Any]] = {}
    position_events: list[dict[str, Any]] = []
    borrowing_events: list[dict[str, Any]] = []
    oracle_events: list[dict[str, Any]] = []
    open_interest_events: list[dict[str, Any]] = []
    impact_pool_events: list[dict[str, Any]] = []
    opening_borrowing: dict[str, Any] = {}
    opening_open_interest_tokens: dict[str, int] = {}
    decode_errors = 0
    for event in event_rows(recording):
        if event["kind"] == "opening_state_checkpoint":
            opening_positions = event["payload"].get("positions", {})
            opening_borrowing = event["payload"].get("borrowing", {})
            opening_open_interest_tokens = event["payload"].get(
                "open_interest_tokens", {}
            )
            for key, order in event["payload"].get("orders", {}).items():
                values = order.get("opening_checkpoint")
                if not isinstance(values, dict):
                    continue
                values = normalize_recorded_order_checkpoint(values)
                opening[key.lower()] = {
                    "event_name": "OpeningOrderCheckpoint",
                    "source": "opening_checkpoint",
                    "values": {**values, "key": key.lower()},
                    "block_number": event["block_number"],
                    "transaction_index": -1,
                    "log_index": -1,
                    "transaction_hash": None,
                }
            continue
        if event["kind"] == "transaction_receipt":
            receipts[event["payload"]["transaction_hash"].lower()] = event
            continue
        if event["kind"] != "gmx_market_log":
            continue
        name = event["payload"].get("event_name")
        if (
            name
            not in {
                "OrderCreated",
                "CumulativeBorrowingFactorUpdated",
                "OraclePriceUpdate",
                "OpenInterestInTokensUpdated",
                "PositionImpactPoolAmountUpdated",
            }
            | POSITION_EVENTS
        ):
            continue
        decoded = _decode_recorded_log(event)
        if decoded is None:
            decode_errors += 1
            continue
        entry = _event_entry(event, decoded)
        if name == "CumulativeBorrowingFactorUpdated":
            borrowing_events.append(entry)
        elif name == "OraclePriceUpdate":
            oracle_events.append(entry)
        elif name == "OpenInterestInTokensUpdated":
            open_interest_events.append(entry)
        elif name == "PositionImpactPoolAmountUpdated":
            impact_pool_events.append(entry)
        elif name == "OrderCreated":
            if decoded.values.get("market") != target_market:
                continue
            key = _order_key(decoded)
            if key is None:
                decode_errors += 1
                continue
            created[key] = entry
        else:
            key = decoded.values.get("orderKey")
            if isinstance(key, str):
                observed[key].append(entry)
            position_events.append(entry)
    _attach_pre_position_state(
        position_events,
        opening_positions,
        borrowing_events,
        opening_borrowing,
        oracle_events,
        index_token,
        open_interest_events,
        opening_open_interest_tokens,
    )
    _attach_pre_impact_pool(position_events, impact_pool_events)
    return (
        created,
        opening,
        observed,
        receipts,
        opening_positions,
        position_events,
        decode_errors,
    )


def _load_terminal_lifecycle(
    recording: Path,
    target_keys: set[str],
    virtual_id: str = "",
    configuration_events: list[dict[str, Any]] | None = None,
    swap_replay: SwapReplay | None = None,
    swap_state_events: list[dict[str, Any]] | None = None,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[tuple[str, int], list[dict[str, Any]]],
    list[dict[str, Any]],
    int,
]:
    lifecycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fee_timeline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    virtual_events: list[dict[str, Any]] = []
    decode_errors = 0
    for log in raw_logs(
        recording,
        LIFECYCLE_EVENTS
        | {
            "KeeperExecutionFee",
            "ExecutionFeeRefund",
            "ExecutionFeeRefundCallback",
            "VirtualPositionInventoryUpdated",
            "SetUint",
            "SetInt",
            "SetBool",
            "PositionFeesInfo",
            "InsolventClose",
            "UiFeeFactorUpdated",
        }
        | STATE_EVENTS,
    ):
        name = event_name_from_data(log.get("data", ""))
        if name not in LIFECYCLE_EVENTS | {
            "KeeperExecutionFee",
            "ExecutionFeeRefund",
            "ExecutionFeeRefundCallback",
            "VirtualPositionInventoryUpdated",
            "SetUint",
            "SetInt",
            "SetBool",
            "PositionFeesInfo",
            "InsolventClose",
            "UiFeeFactorUpdated",
        } | (STATE_EVENTS if swap_replay and swap_replay.available else set()):
            continue
        try:
            decoded = decoded_log(log)
        except (EventDecodeError, KeyError):
            decode_errors += 1
            continue
        key = _order_key(decoded)
        entry = _event_entry_from_log(log, decoded)
        if name in STATE_EVENTS and swap_replay is not None:
            if swap_state_events is not None and swap_replay.wants(
                name, decoded.values
            ):
                swap_state_events.append(entry)
            continue
        if name in {
            "SetUint",
            "SetInt",
            "SetBool",
            "PositionFeesInfo",
            "InsolventClose",
            "UiFeeFactorUpdated",
        }:
            if configuration_events is not None:
                configuration_events.append(entry)
            continue
        if name == "VirtualPositionInventoryUpdated":
            if decoded.values.get("virtualTokenId") == virtual_id:
                virtual_events.append(entry)
            continue
        if name in TERMINAL_EVENTS | {
            "KeeperExecutionFee",
            "ExecutionFeeRefund",
            "ExecutionFeeRefundCallback",
        }:
            fee_timeline[entry["transaction_hash"]].append(entry)
        if key in target_keys and name in LIFECYCLE_EVENTS:
            lifecycle[key].append(entry)
    for entries in lifecycle.values():
        entries.sort(key=_coordinate)
    return (
        lifecycle,
        _associate_execution_fees(fee_timeline),
        virtual_events,
        decode_errors,
    )


def _associate_execution_fees(
    timeline: dict[str, list[dict[str, Any]]],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Fee events follow their order terminal event, including in batched transactions."""
    result: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for transaction_hash, entries in timeline.items():
        current_terminal: tuple[str, int] | None = None
        for entry in sorted(entries, key=_coordinate):
            if entry["event_name"] in TERMINAL_EVENTS:
                current_terminal = (transaction_hash, entry["log_index"])
            elif current_terminal is not None:
                result[current_terminal].append(entry)
    return result


def _load_order_update_topups(
    recording: Path, lifecycle: dict[str, list[dict[str, Any]]], wnt: str
) -> dict[tuple[str, int], int]:
    """Read WNT top-ups from captured update receipts, without relying on event amounts."""
    wanted: dict[str, list[int]] = defaultdict(list)
    for entries in lifecycle.values():
        for entry in entries:
            if entry["event_name"] == "OrderUpdated":
                wanted[entry["transaction_hash"]].append(entry["log_index"])
    if not wanted:
        return {}
    topups = {}
    for transaction_hash, receipt in receipt_rows(recording, wanted):
        for update_index in wanted[transaction_hash]:
            amount = _single_update_topup(receipt.get("logs", []), update_index, wnt)
            if amount is not None:
                topups[transaction_hash, update_index] = amount
    return topups


def _load_execution_receipt_evidence(
    recording: Path, lifecycle: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Decode swap and payout evidence from saved execution receipts."""
    wanted: dict[str, set[str]] = defaultdict(set)
    terminal_receipts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, entries in lifecycle.items():
        for entry in entries:
            if entry["event_name"] == "OrderExecuted":
                wanted[entry["transaction_hash"]].add(key)
                terminal_receipts[entry["transaction_hash"]].append(entry)
    emitter = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))[
        "contracts"
    ]["event_emitter"].lower()
    swaps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    payouts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for transaction_hash, raw_receipt in receipt_rows(recording, wanted):
        terminals = terminal_receipts[transaction_hash]
        if (
            raw_receipt.get("transactionHash") != transaction_hash
            or raw_receipt.get("status") != "0x1"
            or not terminals
            or any(
                raw_receipt.get("blockHash") != e.get("block_hash")
                or raw_receipt.get("blockNumber") != hex(e["block_number"])
                for e in terminals
            )
        ):
            continue
        active_key: str | None = None
        for log in raw_receipt.get("logs", []):
            if log.get("address", "").lower() != emitter:
                topics = log.get("topics", [])
                if (
                    active_key is not None
                    and len(topics) == 3
                    and topics[0].lower() == ERC20_TRANSFER_TOPIC
                ):
                    payouts[active_key].append(
                        {
                            "event_name": "ERC20Transfer",
                            "token": log["address"].lower(),
                            "from": "0x" + topics[1][-40:].lower(),
                            "to": "0x" + topics[2][-40:].lower(),
                            "amount": int(log["data"], 16),
                            "log_index": int(log["logIndex"], 16),
                            "transaction_hash": transaction_hash,
                        }
                    )
                continue
            name = event_name_from_data(log.get("data", ""))
            if name not in {
                "SwapInfo",
                "SwapFeesCollected",
                "PositionDecrease",
                "OrderExecuted",
                "MultichainTransferIn",
            }:
                continue
            decoded = decoded_log(log)
            key = decoded.values.get("orderKey", decoded.values.get("tradeKey"))
            if name == "PositionDecrease" and key in wanted[transaction_hash]:
                active_key = key
                payouts.setdefault(key, [])
            elif name == "OrderExecuted":
                active_key = None
            elif name == "MultichainTransferIn" and active_key is not None:
                payouts[active_key].append(_event_entry_from_log(log, decoded))
            if (
                name in {"SwapInfo", "SwapFeesCollected"}
                and key in wanted[transaction_hash]
            ):
                swaps[key].append(_event_entry_from_log(log, decoded))
    for entries in swaps.values():
        entries.sort(key=_coordinate)
    return swaps, payouts


def _single_update_topup(
    logs: list[dict[str, Any]], update_index: int, wnt: str
) -> int | None:
    order_events = [
        (log, event_name_from_data(log.get("data", "")))
        for log in logs
        if log.get("address", "").lower() != wnt
    ]
    updates = [
        log
        for log, name in order_events
        if name == "OrderUpdated" and int(log["logIndex"], 16) == update_index
    ]
    if len(updates) != 1:
        return None
    prior_order_index = max(
        (
            int(log["logIndex"], 16)
            for log, name in order_events
            if name
            in {
                "OrderCreated",
                "OrderUpdated",
                "OrderExecuted",
                "OrderCancelled",
                "OrderFrozen",
            }
            and int(log["logIndex"], 16) < update_index
        ),
        default=-1,
    )
    transfers = [
        log
        for log in logs
        if log.get("address", "").lower() == wnt
        and len(log.get("topics", [])) == 3
        and log["topics"][0].lower() == ERC20_TRANSFER_TOPIC
        and log["topics"][2][-40:].lower() == ARBITRUM_ORDER_VAULT[2:]
        and prior_order_index < int(log["logIndex"], 16) < update_index
    ]
    if len(transfers) > 1:
        return None
    return int(transfers[0]["data"], 16) if transfers else 0


def _iter_raw_event_emitter_logs(recording):
    yield from raw_logs(recording)
