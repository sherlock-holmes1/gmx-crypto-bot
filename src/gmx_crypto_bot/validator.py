"""Read-only observed-order lifecycle and execution validation for GMX recordings."""
from __future__ import annotations

import argparse
import base64
import gzip
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from gmx_crypto_bot.checkpoint import normalize_recorded_order_checkpoint
from gmx_crypto_bot.event_decoder import DecodedEventLog, EventDecodeError, decode_event_log, event_name_from_data


TERMINAL_EVENTS = {"OrderExecuted", "OrderCancelled", "OrderFrozen"}
LIFECYCLE_EVENTS = TERMINAL_EVENTS | {"OrderUpdated", "OrderSizeDeltaAutoUpdated", "OrderCollateralDeltaAmountAutoUpdated"}
POSITION_EVENTS = {"PositionIncrease", "PositionDecrease", "PositionFeesCollected"}
INCREASE_ORDER_TYPES = {2, 3, 8}
DECREASE_ORDER_TYPES = {4, 5, 6, 7}
MAX_UINT256 = (1 << 256) - 1
FLOAT_PRECISION = 10**30
FUNDING_PRECISION = 10**45
ARBITRUM_ORDER_VAULT = "0x31ef83a530fde1b38ee9a18093a333d8bbbc40d5"
ARBITRUM_MULTICHAIN_VAULT = "0xceaadfaf6a8c489b250e407987877c5fdfcdbe6e"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UNMODELED_ECONOMICS = (
    "historical_configuration_factors",
    "independent_price_impact",
    "execution_fee_gas_and_transfer_proof",
    "liquidation_settlement",
)


@dataclass(frozen=True)
class ValidationReport:
    schema: str
    version: int
    recording: str
    orders_created: int
    opening_terminal_orders: int
    terminal_orders: int
    matched: int
    mismatched: int
    unresolved: int
    decode_errors: int
    implemented_checks_pass: bool
    economic_calibration_complete: bool
    complete: bool
    mismatch_counts: dict[str, int]
    check_counts: dict[str, dict[str, int]]
    remaining_economic_checks: list[str]
    orders: list[dict[str, Any]]


def validate_orders(recording: Path) -> ValidationReport:
    """Join target order requests to raw terminal events and observed execution events."""
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    target_market = metadata["market"]["market_token_address"].lower()
    index_token = metadata.get("tokens", {}).get("index", {}).get("address", "").lower()
    created, opening, observed, receipts, opening_positions, position_events, decode_errors = _load_replay_evidence(
        recording, target_market, index_token
    )
    lifecycle, execution_fees, raw_decode_errors = _load_terminal_lifecycle(recording, set(created) | set(opening))
    update_topups = _load_order_update_topups(recording, lifecycle, index_token)
    swaps, payouts = _load_execution_receipt_evidence(recording, lifecycle)
    decode_errors += raw_decode_errors
    results: list[dict[str, Any]] = []
    mismatch_counts: Counter[str] = Counter()

    for key, request in sorted(created.items(), key=lambda item: _coordinate(item[1])):
        order = _validate_order(key, request, lifecycle.get(key, []), observed.get(key, []), receipts,
                                execution_fees, update_topups, opening_positions, position_events,
                                swaps.get(key, []), payouts.get(key, []), metadata)
        results.append(order)
        mismatch_counts.update(order["mismatches"])
    opening_terminal_orders = 0
    for key, request in sorted(opening.items()):
        if key in created or not any(entry["event_name"] in TERMINAL_EVENTS for entry in lifecycle.get(key, [])):
            continue
        order = _validate_order(key, request, lifecycle[key], observed.get(key, []), receipts,
                                execution_fees, update_topups, opening_positions, position_events,
                                swaps.get(key, []), payouts.get(key, []), metadata)
        results.append(order)
        mismatch_counts.update(order["mismatches"])
        opening_terminal_orders += 1

    matched = sum(order["status"] == "matched" for order in results)
    mismatched = sum(order["status"] == "mismatch" for order in results)
    unresolved = sum(order["status"] == "unresolved" for order in results)
    check_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for order in results:
        for check, outcome in order["checks"].items():
            check_counts[check][outcome] += 1
    remaining_checks = sorted({
        check for check, counts in check_counts.items() if counts.get("unavailable", 0)
    } | set(UNMODELED_ECONOMICS) | (
        {"ambiguous_order_update_topups"}
        if check_counts["execution_fee_event_balance"].get("unavailable_update_receipt", 0) else set()
    ))
    implemented_checks_pass = not mismatched and not decode_errors
    economic_calibration_complete = not remaining_checks and implemented_checks_pass
    return ValidationReport(
        schema="GmxObservedOrderValidationReport",
        version=2,
        recording=str(recording),
        orders_created=len(created),
        opening_terminal_orders=opening_terminal_orders,
        terminal_orders=len(results) - unresolved,
        matched=matched,
        mismatched=mismatched,
        unresolved=unresolved,
        decode_errors=decode_errors,
        implemented_checks_pass=implemented_checks_pass,
        economic_calibration_complete=economic_calibration_complete,
        complete=economic_calibration_complete,
        mismatch_counts=dict(sorted(mismatch_counts.items())),
        check_counts={key: dict(sorted(counts.items())) for key, counts in sorted(check_counts.items())},
        remaining_economic_checks=remaining_checks,
        orders=results,
    )


def _load_replay_evidence(
    recording: Path, target_market: str, index_token: str
) -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]],
    dict[str, dict[str, Any]], list[dict[str, Any]], int,
]:
    created: dict[str, dict[str, Any]] = {}
    opening: dict[str, dict[str, Any]] = {}
    observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    receipts: dict[str, dict[str, Any]] = {}
    opening_positions: dict[str, dict[str, Any]] = {}
    position_events: list[dict[str, Any]] = []
    borrowing_events: list[dict[str, Any]] = []
    oracle_events: list[dict[str, Any]] = []
    opening_borrowing: dict[str, Any] = {}
    decode_errors = 0
    with (recording / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event["kind"] == "opening_state_checkpoint":
                opening_positions = event["payload"].get("positions", {})
                opening_borrowing = event["payload"].get("borrowing", {})
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
            if name not in {"OrderCreated", "CumulativeBorrowingFactorUpdated", "OraclePriceUpdate"} | POSITION_EVENTS:
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
    _attach_pre_position_state(position_events, opening_positions, borrowing_events, opening_borrowing,
                               oracle_events, index_token)
    return created, opening, observed, receipts, opening_positions, position_events, decode_errors


def _attach_pre_position_state(
    events: list[dict[str, Any]], opening_positions: dict[str, dict[str, Any]],
    borrowing_events: list[dict[str, Any]], opening_borrowing: dict[str, Any],
    oracle_events: list[dict[str, Any]] | None = None, index_token: str | None = None,
) -> None:
    """Reconstruct the position immediately before each observed position event."""
    positions = {key.lower(): dict(value) for key, value in opening_positions.items()}
    borrowing = {
        side: opening_borrowing.get(side, {}).get("cumulative_factor", {}).get("nextCumulativeBorrowingFactor")
        for side in ("long", "short")
    }
    oracle_at_transaction: dict[str, dict[str, Any]] = {}
    oracle_transaction: str | None = None
    for entry in sorted(events + borrowing_events + (oracle_events or []), key=_coordinate):
        values = entry["values"]
        if entry["transaction_hash"] != oracle_transaction:
            oracle_transaction = entry["transaction_hash"]
            oracle_at_transaction = {}
        if entry["event_name"] == "OraclePriceUpdate":
            token = values.get("token")
            if isinstance(token, str):
                oracle_at_transaction[token] = values
            continue
        if entry["event_name"] == "CumulativeBorrowingFactorUpdated":
            borrowing["long" if values.get("isLong") else "short"] = values.get("nextValue")
            continue
        key = values.get("positionKey")
        if not isinstance(key, str):
            continue
        key = key.lower()
        previous = positions.get(key)
        entry["pre_position"] = dict(previous) if previous is not None else None
        oracle = oracle_at_transaction.get(index_token) if index_token else next(reversed(oracle_at_transaction.values()), None)
        entry["oracle_at_event"] = dict(oracle) if oracle is not None else None
        entry["oracle_prices_at_event"] = {token: dict(price) for token, price in oracle_at_transaction.items()}
        is_long = previous.get("isLong") if previous is not None else values.get("isLong")
        entry["cumulative_borrowing_factor"] = borrowing["long" if is_long else "short"]
        if entry["event_name"] == "PositionFeesCollected":
            continue
        if not values.get("sizeInUsd"):
            positions.pop(key, None)
            continue
        next_position = dict(previous or {})
        for field in (
            "account", "market", "collateralToken", "isLong", "sizeInUsd", "sizeInTokens",
            "collateralAmount", "borrowingFactor", "fundingFeeAmountPerSize",
            "longTokenClaimableFundingAmountPerSize", "shortTokenClaimableFundingAmountPerSize",
            "pendingImpactAmount",
        ):
            if field in values:
                next_position[field] = values[field]
        old_pending = (previous or {}).get("pendingImpactAmount", 0)
        if entry["event_name"] == "PositionIncrease" and isinstance(values.get("pendingPriceImpactAmount"), int):
            next_position["pendingImpactAmount"] = old_pending + values["pendingPriceImpactAmount"]
        elif entry["event_name"] == "PositionDecrease" and previous is not None:
            size = previous.get("sizeInUsd")
            delta = values.get("sizeDeltaUsd")
            if isinstance(old_pending, int) and isinstance(size, int) and size > 0 and isinstance(delta, int):
                next_position["pendingImpactAmount"] = old_pending - _proportional_pending_impact(
                    old_pending, delta, size
                )
        positions[key] = next_position


def _load_terminal_lifecycle(
    recording: Path, target_keys: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, int], list[dict[str, Any]]], int]:
    lifecycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fee_timeline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decode_errors = 0
    for log in _iter_raw_event_emitter_logs(recording):
        name = event_name_from_data(log.get("data", ""))
        if name not in LIFECYCLE_EVENTS | {"KeeperExecutionFee", "ExecutionFeeRefund", "ExecutionFeeRefundCallback"}:
            continue
        try:
            decoded = decode_event_log(log["data"])
        except (EventDecodeError, KeyError):
            decode_errors += 1
            continue
        key = _order_key(decoded)
        entry = _event_entry_from_log(log, decoded)
        if name in TERMINAL_EVENTS | {"KeeperExecutionFee", "ExecutionFeeRefund", "ExecutionFeeRefundCallback"}:
            fee_timeline[entry["transaction_hash"]].append(entry)
        if key in target_keys and name in LIFECYCLE_EVENTS:
            lifecycle[key].append(entry)
    for entries in lifecycle.values():
        entries.sort(key=_coordinate)
    return lifecycle, _associate_execution_fees(fee_timeline), decode_errors


def _associate_execution_fees(
    timeline: dict[str, list[dict[str, Any]]]
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
    sequences: dict[int, str] = {}
    bundles: set[str] = set()
    with (recording / "raw" / "manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item.get("source") != "rpc-eth_getTransactionReceipt" or item.get("kind") != "response":
                continue
            params = item.get("request", {}).get("params", [])
            if params and params[0].lower() in wanted:
                sequences[item["seq"]] = params[0].lower()
                bundles.add(item["bundle"])
    topups: dict[tuple[str, int], int] = {}
    for bundle in sorted(bundles):
        with gzip.open(recording / bundle, "rt", encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                transaction_hash = sequences.get(item["seq"])
                if transaction_hash is None:
                    continue
                response = json.loads(base64.b64decode(item["body_base64"]))
                logs = response.get("result", {}).get("logs", [])
                for update_index in wanted[transaction_hash]:
                    amount = _single_update_topup(logs, update_index, wnt)
                    if amount is not None:
                        topups[(transaction_hash, update_index)] = amount
    return topups


def _load_execution_receipt_evidence(
    recording: Path, lifecycle: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Decode swap and payout evidence from saved execution receipts."""
    wanted: dict[str, set[str]] = defaultdict(set)
    for key, entries in lifecycle.items():
        for entry in entries:
            if entry["event_name"] == "OrderExecuted":
                wanted[entry["transaction_hash"]].add(key)
    sequences: dict[int, str] = {}
    bundles: set[str] = set()
    with (recording / "raw" / "manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item.get("source") != "rpc-eth_getTransactionReceipt" or item.get("kind") != "response":
                continue
            params = item.get("request", {}).get("params", [])
            if params and params[0].lower() in wanted:
                sequences[item["seq"]] = params[0].lower()
                bundles.add(item["bundle"])
    emitter = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))["contracts"]["event_emitter"].lower()
    swaps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    payouts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bundle in sorted(bundles):
        with gzip.open(recording / bundle, "rt", encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                transaction_hash = sequences.get(item["seq"])
                if transaction_hash is None:
                    continue
                response = json.loads(base64.b64decode(item["body_base64"]))
                active_key: str | None = None
                for log in response.get("result", {}).get("logs", []):
                    if log.get("address", "").lower() != emitter:
                        topics = log.get("topics", [])
                        if (active_key is not None and len(topics) == 3
                                and topics[0].lower() == ERC20_TRANSFER_TOPIC):
                            payouts[active_key].append({
                                "event_name": "ERC20Transfer",
                                "token": log["address"].lower(),
                                "from": "0x" + topics[1][-40:].lower(),
                                "to": "0x" + topics[2][-40:].lower(),
                                "amount": int(log["data"], 16),
                                "log_index": int(log["logIndex"], 16),
                                "transaction_hash": transaction_hash,
                            })
                        continue
                    name = event_name_from_data(log.get("data", ""))
                    if name not in {"SwapInfo", "SwapFeesCollected", "PositionDecrease", "OrderExecuted", "MultichainTransferIn"}:
                        continue
                    decoded = decode_event_log(log["data"])
                    key = decoded.values.get("orderKey", decoded.values.get("tradeKey"))
                    if name == "PositionDecrease" and key in wanted[transaction_hash]:
                        active_key = key
                    elif name == "OrderExecuted":
                        active_key = None
                    elif name == "MultichainTransferIn" and active_key is not None:
                        payouts[active_key].append(_event_entry_from_log(log, decoded))
                    if name in {"SwapInfo", "SwapFeesCollected"} and key in wanted[transaction_hash]:
                        swaps[key].append(_event_entry_from_log(log, decoded))
    for entries in swaps.values():
        entries.sort(key=_coordinate)
    return swaps, payouts


def _single_update_topup(logs: list[dict[str, Any]], update_index: int, wnt: str) -> int | None:
    order_events = [
        (log, event_name_from_data(log.get("data", "")))
        for log in logs
        if log.get("address", "").lower() != wnt
    ]
    updates = [log for log, name in order_events if name == "OrderUpdated" and int(log["logIndex"], 16) == update_index]
    if len(updates) != 1:
        return None
    prior_order_index = max((
        int(log["logIndex"], 16) for log, name in order_events
        if name in {"OrderCreated", "OrderUpdated", "OrderExecuted", "OrderCancelled", "OrderFrozen"}
        and int(log["logIndex"], 16) < update_index
    ), default=-1)
    transfers = [
        log for log in logs
        if log.get("address", "").lower() == wnt
        and len(log.get("topics", [])) == 3
        and log["topics"][0].lower() == ERC20_TRANSFER_TOPIC
        and log["topics"][2][-40:].lower() == ARBITRUM_ORDER_VAULT[2:]
        and prior_order_index < int(log["logIndex"], 16) < update_index
    ]
    if len(transfers) > 1:
        return None
    return int(transfers[0]["data"], 16) if transfers else 0


def _iter_raw_event_emitter_logs(recording: Path) -> Iterator[dict[str, Any]]:
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    emitter = metadata["contracts"]["event_emitter"].lower()
    wanted_sequences = _event_emitter_response_sequences(recording, emitter)
    for bundle in sorted((recording / "raw").glob("rpc-*.jsonl.gz")):
        with gzip.open(bundle, "rt", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record["seq"] not in wanted_sequences:
                    continue
                response = json.loads(base64.b64decode(record["body_base64"]))
                if not isinstance(response, dict):
                    continue
                logs = response.get("result")
                if not isinstance(logs, list):
                    continue
                for log in logs:
                    if isinstance(log, dict) and log.get("address", "").lower() == emitter:
                        yield log


def _event_emitter_response_sequences(recording: Path, emitter: str) -> set[int]:
    sequences: set[int] = set()
    with (recording / "raw" / "manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            if entry.get("kind") != "response" or entry.get("source") != "rpc-eth_getLogs":
                continue
            params = entry.get("request", {}).get("params", [])
            if not params or not isinstance(params[0], dict):
                continue
            if params[0].get("address", "").lower() == emitter:
                sequences.add(entry["seq"])
    return sequences


def _validate_order(
    key: str,
    request: dict[str, Any],
    lifecycle: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    receipts: dict[str, dict[str, Any]],
    execution_fees: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
    update_topups: dict[tuple[str, int], int] | None = None,
    opening_positions: dict[str, dict[str, Any]] | None = None,
    position_events: list[dict[str, Any]] | None = None,
    swap_events: list[dict[str, Any]] | None = None,
    payout_events: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updates = [entry for entry in lifecycle if entry["event_name"] not in TERMINAL_EVENTS]
    terminals = [entry for entry in lifecycle if entry["event_name"] in TERMINAL_EVENTS]
    final_request = dict(request["values"])
    for update in updates:
        if update["event_name"] == "OrderSizeDeltaAutoUpdated":
            final_request["sizeDeltaUsd"] = update["values"]["nextSizeDeltaUsd"]
        elif update["event_name"] == "OrderCollateralDeltaAmountAutoUpdated":
            final_request["initialCollateralDeltaAmount"] = update["values"]["nextCollateralDeltaAmount"]
        else:
            final_request.update(update["values"])
    mismatches: list[str] = []
    checks: dict[str, str] = {}
    modeled_settlement: dict[str, Any] | None = None
    if not terminals:
        return _order_result(key, request, final_request, None, observed, checks, ["no_terminal_event_in_window"], "unresolved")
    terminal = terminals[-1]
    fee_events = (execution_fees or {}).get((terminal["transaction_hash"], terminal["log_index"]), [])
    _compare_execution_fee_events(
        final_request,
        fee_events,
        checks, mismatches,
        updates=updates, update_topups=update_topups or {},
    )
    if len(terminals) != 1:
        mismatches.append("multiple_terminal_events")
    checks["terminal_after_request"] = _comparison(_coordinate(terminal) > _coordinate(request), "terminal_before_request", mismatches)
    checks["account"] = _comparison(
        terminal["values"].get("account") == final_request.get("account"), "terminal_account_mismatch", mismatches
    )
    if terminal["event_name"] in {"OrderCancelled", "OrderFrozen"}:
        reason = terminal["values"].get("reason")
        reason_bytes = terminal["values"].get("reasonBytes")
        checks["terminal_reason"] = _comparison(
            (isinstance(reason, str) and bool(reason))
            or (isinstance(reason_bytes, str) and reason_bytes not in {"", "0x"}),
            "missing_terminal_reason", mismatches
        )
        checks["terminal_reason_reconstruction"] = _reconstruct_terminal_reason(
            final_request, terminal, fee_events, opening_positions, position_events, mismatches
        )
    if terminal["event_name"] == "OrderExecuted":
        related = observed
        position = next((entry for entry in related if entry["event_name"] in {"PositionIncrease", "PositionDecrease"}), None)
        fees = next((entry for entry in related if entry["event_name"] == "PositionFeesCollected"), None)
        if position is None:
            mismatches.append("missing_position_execution_event")
            checks["position_execution"] = "mismatch"
        else:
            checks["position_execution"] = "matched"
            _compare_execution(final_request, position, checks, mismatches)
            checks["position_transaction"] = _comparison(
                position["transaction_hash"] == terminal["transaction_hash"],
                "position_transaction_mismatch", mismatches,
            )
            expected_increase = final_request.get("orderType") in INCREASE_ORDER_TYPES
            expected_decrease = final_request.get("orderType") in DECREASE_ORDER_TYPES
            checks["order_type"] = _comparison(
                (expected_increase and position["event_name"] == "PositionIncrease")
                or (expected_decrease and position["event_name"] == "PositionDecrease"),
                "order_type_mismatch", mismatches,
            )
            checks["direction"] = _comparison(
                position["values"].get("isLong") == final_request.get("isLong"),
                "direction_mismatch", mismatches,
            )
            checks["collateral_token"] = (
                "matched" if position["values"].get("collateralToken") == final_request.get("initialCollateralToken")
                else "different_from_input"
            )
            _compare_position_math(position, checks, mismatches)
        if fees is not None and position is not None:
            checks["fee_collateral_token"] = _comparison(
                fees["values"].get("collateralToken") == position["values"].get("collateralToken"),
                "fee_collateral_token_mismatch", mismatches,
            )
            checks["fee_transaction"] = _comparison(
                fees["transaction_hash"] == terminal["transaction_hash"],
                "fee_transaction_mismatch", mismatches,
            )
            checks["fee_trade_size"] = _comparison(
                fees["values"].get("tradeSizeUsd") == position["values"].get("sizeDeltaUsd"),
                "fee_trade_size_mismatch",
                mismatches,
            )
            _compare_fee_math(final_request, position, fees, checks, mismatches)
            if position["event_name"] == "PositionIncrease":
                _compare_collateral_conversion(final_request, position, fees, swap_events or [], checks, mismatches)
            elif final_request.get("orderType") != 7:
                modeled_settlement = _compare_decrease_settlement(
                    final_request, position, fees, swap_events or [], payout_events or [],
                    metadata or {}, checks, mismatches,
                )
        elif fees is None:
            checks["position_fees"] = "unavailable"
    receipt = receipts.get(terminal["transaction_hash"])
    if receipt is None:
        checks["receipt"] = "unavailable"
    else:
        checks["receipt"] = _comparison(
            receipt["payload"].get("status") == "0x1", "failed_terminal_receipt", mismatches
        )
        gas_used = receipt["payload"].get("gas_used")
        checks["gas_used"] = _comparison(
            isinstance(gas_used, str) and gas_used.startswith("0x")
            and all(character in "0123456789abcdefABCDEF" for character in gas_used[2:])
            and int(gas_used, 16) > 0,
            "missing_terminal_gas", mismatches,
        )
    status = "mismatch" if mismatches else "matched"
    result = _order_result(key, request, final_request, terminal, observed, checks, mismatches, status)
    result["observed_execution_fee_events"] = fee_events
    result["observed_swap_events"] = swap_events or []
    result["observed_payout_events"] = payout_events or []
    if modeled_settlement is not None:
        result["modeled_decrease_settlement"] = modeled_settlement
    result["observed_order_update_topups"] = [
        {"transaction_hash": entry["transaction_hash"], "log_index": entry["log_index"],
         "amount": (update_topups or {}).get((entry["transaction_hash"], entry["log_index"]))}
        for entry in updates if entry["event_name"] == "OrderUpdated"
    ]
    return result


def _compare_execution_fee_events(
    request: dict[str, Any], events: list[dict[str, Any]],
    checks: dict[str, str], mismatches: list[str], *,
    updates: list[dict[str, Any]] | None = None,
    update_topups: dict[tuple[str, int], int] | None = None,
) -> None:
    fee = request.get("executionFee")
    keeper = [entry for entry in events if entry["event_name"] == "KeeperExecutionFee"]
    refund = [entry for entry in events if entry["event_name"] in {"ExecutionFeeRefund", "ExecutionFeeRefundCallback"}]
    if fee == 0 and not keeper and not refund:
        checks["execution_fee_event_balance"] = "not_applicable"
        return
    if not isinstance(fee, int) or len(keeper) != 1 or len(refund) > 1:
        checks["execution_fee_event_balance"] = "unavailable"
        return
    keeper_amount = keeper[0]["values"].get("executionFeeAmount")
    refund_amount = refund[0]["values"].get("refundFeeAmount") if refund else 0
    if not isinstance(keeper_amount, int) or not isinstance(refund_amount, int):
        checks["execution_fee_event_balance"] = "unavailable"
        return
    paid = keeper_amount + refund_amount
    if keeper_amount >= 0 and refund_amount >= 0 and paid == fee:
        checks["execution_fee_event_balance"] = "matched"
        return
    update_entries = [entry for entry in updates or [] if entry["event_name"] == "OrderUpdated"]
    topups = update_topups or {}
    if any((entry["transaction_hash"], entry["log_index"]) not in topups for entry in update_entries):
        checks["execution_fee_event_balance"] = "unavailable_update_receipt"
        return
    fee += sum(topups[(entry["transaction_hash"], entry["log_index"])] for entry in update_entries)
    if update_entries and paid != fee:
        checks["execution_fee_event_balance"] = "unavailable_update_receipt"
        return
    checks["execution_fee_event_balance"] = _comparison(
        keeper_amount >= 0 and refund_amount >= 0 and paid == fee,
        "execution_fee_event_balance_mismatch", mismatches,
    )


def _reconstruct_terminal_reason(
    request: dict[str, Any], terminal: dict[str, Any], fee_events: list[dict[str, Any]],
    opening_positions: dict[str, dict[str, Any]] | None,
    position_events: list[dict[str, Any]] | None, mismatches: list[str],
) -> str:
    """Check the cancellation cause against evidence beyond the reason label."""
    if terminal["event_name"] != "OrderCancelled":
        return "unavailable"
    reason = terminal["values"].get("reason")
    reason_bytes = terminal["values"].get("reasonBytes", "0x")
    if reason == "USER_INITIATED_CANCEL" and reason_bytes == "0x":
        keepers = [event["values"].get("keeper") for event in fee_events
                   if event["event_name"] == "KeeperExecutionFee"]
        if not keepers:
            return "unavailable"
        return _comparison(len(keepers) == 1 and keepers[0] == request.get("account"),
                           "user_cancel_keeper_mismatch", mismatches)
    if reason == "AUTO_CANCEL" and reason_bytes == "0x":
        if position_events is None:
            return "unavailable"
        identity = _request_position_identity(request)
        closed = any(
            event["event_name"] == "PositionDecrease"
            and event["transaction_hash"] == terminal["transaction_hash"]
            and _coordinate(event) < _coordinate(terminal)
            and _position_identity(event["values"]) == identity
            and event["values"].get("sizeInUsd") == 0
            for event in position_events
        )
        return _comparison(request.get("autoCancel") is True
                           and request.get("orderType") in {5, 6} and closed,
                           "auto_cancel_without_matching_position_close", mismatches)
    if reason == "" and reason_bytes == "0x4dfbbff3":  # EmptyPosition()
        if opening_positions is None or position_events is None:
            return "unavailable"
        identity = _request_position_identity(request)
        state = next((position.get("sizeInUsd") for position in opening_positions.values()
                      if _position_identity(position) == identity), None)
        for event in position_events:
            if _coordinate(event) >= _coordinate(terminal):
                continue
            if event["event_name"] not in {"PositionIncrease", "PositionDecrease"}:
                continue
            if _position_identity(event["values"]) == identity:
                state = event["values"].get("sizeInUsd")
        return _comparison(request.get("orderType") == 4 and not state,
                           "empty_position_cancel_with_active_position", mismatches)
    if reason == "" and isinstance(reason_bytes, str) and reason_bytes.startswith("0xe09ad0e9"):
        # OrderNotFulfillableAtAcceptablePrice(uint256,uint256).
        if len(reason_bytes) != 2 + 8 + 64 * 2:
            return "unavailable"
        price = int(reason_bytes[10:74], 16)
        acceptable = int(reason_bytes[74:138], 16)
        buying_index = (request.get("isLong") == (request.get("orderType") in INCREASE_ORDER_TYPES))
        violated = price > acceptable if buying_index else price < acceptable
        return _comparison(acceptable == request.get("acceptablePrice") and violated,
                           "acceptable_price_cancel_reason_mismatch", mismatches)
    return "unavailable"


def _position_identity(values: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (values.get("account"), values.get("market"), values.get("collateralToken"), values.get("isLong"))


def _request_position_identity(values: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (values.get("account"), values.get("market"), values.get("initialCollateralToken"), values.get("isLong"))


def _compare_collateral_conversion(
    request: dict[str, Any], position: dict[str, Any], fees: dict[str, Any],
    swap_events: list[dict[str, Any]], checks: dict[str, str], mismatches: list[str],
) -> None:
    """Reconcile the recorded swap path and recompute each hop's token output."""
    hops = [event for event in swap_events if event["event_name"] == "SwapInfo"]
    output_token = position["values"].get("collateralToken")
    if not hops:
        checks["collateral_conversion"] = (
            "not_applicable" if output_token == request.get("initialCollateralToken") else "unavailable"
        )
        return
    chain_valid = True
    arithmetic_valid = True
    token = request.get("initialCollateralToken")
    amount = request.get("initialCollateralDeltaAmount")
    for hop in hops:
        values = hop["values"]
        chain_valid &= values.get("tokenIn") == token and values.get("amountIn") == amount
        token, amount = values.get("tokenOut"), values.get("amountOut")
        after_fees = values.get("amountInAfterFees")
        price_in = values.get("tokenInPrice")
        price_out = values.get("tokenOutPrice")
        impact = values.get("priceImpactAmount")
        input_impact = values.get("tokenInPriceImpactAmount")
        if not all(isinstance(value, int) for value in (after_fees, price_in, price_out, impact, input_impact, amount)) or price_out <= 0:
            arithmetic_valid = False
            continue
        effective_input = after_fees + input_impact + min(impact, 0)
        arithmetic_valid &= effective_input >= 0 and amount == effective_input * price_in // price_out + max(impact, 0)
    collateral_delta = position["values"].get("collateralDeltaAmount")
    total_cost = fees["values"].get("totalCostAmount")
    chain_valid &= token == output_token and all(isinstance(value, int) for value in (amount, collateral_delta, total_cost))
    if isinstance(amount, int) and isinstance(collateral_delta, int) and isinstance(total_cost, int):
        chain_valid &= amount == collateral_delta + total_cost
    minimum = request.get("minOutputAmount")
    if isinstance(minimum, int) and isinstance(amount, int):
        chain_valid &= amount >= minimum
    checks["collateral_conversion"] = _comparison(chain_valid, "collateral_conversion_mismatch", mismatches)
    checks["swap_output_arithmetic"] = _comparison(arithmetic_valid, "swap_output_arithmetic_mismatch", mismatches)


def _model_decrease_settlement(
    request: dict[str, Any], position: dict[str, Any], fees: dict[str, Any],
    swap_events: list[dict[str, Any]], metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply GMX's ordered decrease costs and output swaps using recorded factors."""
    previous = position.get("pre_position")
    values = position["values"]
    fee_values = fees["values"]
    if not isinstance(previous, dict):
        return None
    collateral_token = values.get("collateralToken")
    pnl_side = "long" if values.get("isLong") else "short"
    pnl_token = metadata.get("tokens", {}).get(pnl_side, {}).get("address", "").lower()
    oracle_prices = position.get("oracle_prices_at_event", {})
    pnl_oracle = oracle_prices.get(pnl_token)
    collateral_oracle = oracle_prices.get(collateral_token)
    collateral_price = fee_values.get("collateralTokenPrice.min")
    if pnl_token == collateral_token:
        pnl_min = collateral_price
        pnl_max = fee_values.get("collateralTokenPrice.max")
    else:
        pnl_min = pnl_oracle.get("minPrice") if isinstance(pnl_oracle, dict) else None
        pnl_max = pnl_oracle.get("maxPrice") if isinstance(pnl_oracle, dict) else None
    base = values.get("basePnlUsd")
    impact = values.get("totalImpactUsd")
    funding = fee_values.get("fundingFeeAmount")
    total_fee = fee_values.get("totalCostAmount")
    collateral = previous.get("collateralAmount")
    if not all(isinstance(value, int) for value in (
        pnl_min, pnl_max, collateral_price, base, impact, funding, total_fee, collateral
    )) or min(pnl_min, pnl_max, collateral_price) <= 0 or total_fee < funding:
        return None
    output = 0
    secondary = 0
    if base > 0:
        profit = base // pnl_max
        if collateral_token == pnl_token:
            output += profit
        else:
            secondary += profit
    if impact > 0:
        profit = impact // pnl_max
        if collateral_token == pnl_token:
            output += profit
        else:
            secondary += profit
    hops = [event for event in swap_events if event["event_name"] == "SwapInfo"]
    pre_hops = [event for event in hops if _coordinate(event) < _coordinate(position)]
    post_hops = [event for event in hops if _coordinate(event) > _coordinate(position)]
    swaps_valid = True
    if request.get("decreasePositionSwapType") == 1 and secondary > 0 and pre_hops:
        hop = pre_hops[0]["values"]
        swaps_valid &= len(pre_hops) == 1 and hop.get("tokenIn") == pnl_token
        swaps_valid &= hop.get("amountIn") == secondary and hop.get("tokenOut") == collateral_token
        swaps_valid &= hop.get("receiver") == request.get("market")
        if not isinstance(hop.get("amountOut"), int):
            return None
        output += hop["amountOut"]
        secondary = 0
    elif pre_hops:
        swaps_valid = False

    def pay_cost(cost_usd: int) -> bool:
        nonlocal output, collateral, secondary
        if cost_usd <= 0:
            return True
        needed = _ceil_div(cost_usd, collateral_price)
        taken = min(output, needed)
        output -= taken
        needed -= taken
        taken = min(collateral, needed)
        collateral -= taken
        needed -= taken
        if needed == 0:
            return True
        secondary_needed = needed * collateral_price // pnl_min
        taken = min(secondary, secondary_needed)
        secondary -= taken
        return taken == secondary_needed

    costs_paid = all((
        pay_cost(funding * collateral_price),
        pay_cost(-base) if base < 0 else True,
        pay_cost((total_fee - funding) * collateral_price),
        pay_cost(-impact) if impact < 0 else True,
        pay_cost(values.get("values.priceImpactDiffUsd", 0)),
    ))
    requested_withdrawal = request.get("initialCollateralDeltaAmount")
    old_size = previous.get("sizeInUsd")
    size_delta = values.get("sizeDeltaUsd")
    if not isinstance(requested_withdrawal, int) or not isinstance(old_size, int) or not isinstance(size_delta, int):
        return None
    withdrawal = 0 if size_delta == old_size else requested_withdrawal
    if withdrawal > collateral:
        return None
    collateral -= withdrawal
    output += withdrawal
    closed = values.get("sizeInUsd") == 0 or values.get("sizeInTokens") == 0
    if closed:
        output += collateral
        collateral = 0
    output_token = collateral_token
    if request.get("decreasePositionSwapType") == 2 and output > 0 and post_hops:
        hop = post_hops[0]["values"]
        if (hop.get("tokenIn") == collateral_token and hop.get("amountIn") == output
                and hop.get("tokenOut") == pnl_token and hop.get("receiver") == request.get("market")):
            if not isinstance(hop.get("amountOut"), int):
                return None
            output = secondary + hop["amountOut"]
            secondary = 0
            output_token = pnl_token
            post_hops = post_hops[1:]
        # A failed optional swap leaves the original output tokens unchanged.
    if secondary == 0:
        for event in post_hops:
            hop = event["values"]
            if hop.get("tokenIn") != output_token or hop.get("amountIn") != output:
                swaps_valid = False
                break
            if not isinstance(hop.get("amountOut"), int):
                return None
            output_token, output = hop.get("tokenOut"), hop["amountOut"]
    elif post_hops:
        swaps_valid = False
    return {
        "remaining_collateral": collateral,
        "output_token": output_token,
        "output_amount": output,
        "secondary_output_token": pnl_token,
        "secondary_output_amount": secondary,
        "net_realized_pnl_usd": base + impact - total_fee * collateral_price,
        "costs_paid": costs_paid,
        "swaps_valid": swaps_valid,
        "collateral_price": collateral_price,
        "collateral_oracle": collateral_oracle,
        "oracle_prices": oracle_prices,
    }


def _compare_decrease_settlement(
    request: dict[str, Any], position: dict[str, Any], fees: dict[str, Any],
    swap_events: list[dict[str, Any]], payout_events: list[dict[str, Any]],
    metadata: dict[str, Any], checks: dict[str, str], mismatches: list[str],
) -> dict[str, Any] | None:
    model = _model_decrease_settlement(request, position, fees, swap_events, metadata)
    if model is None:
        for check in ("decrease_collateral_and_cash", "net_realized_pnl", "output_amounts"):
            checks[check] = "unavailable"
        return None
    values = position["values"]
    previous = position["pre_position"]
    collateral_matches = (
        model["costs_paid"] and model["swaps_valid"]
        and values.get("collateralAmount") == model["remaining_collateral"]
        and values.get("collateralDeltaAmount") == previous["collateralAmount"] - model["remaining_collateral"]
    )
    checks["decrease_collateral_and_cash"] = _comparison(
        collateral_matches, "decrease_collateral_mismatch", mismatches,
    )
    expected = Counter()
    if model["output_amount"] > 0:
        expected[(model["output_token"], model["output_amount"])] += 1
    if model["secondary_output_amount"] > 0:
        expected[(model["secondary_output_token"], model["secondary_output_amount"])] += 1
    recorded_multichain = [
        event for event in payout_events
        if event["event_name"] == "MultichainTransferIn"
        and event["values"].get("account") == request.get("receiver")
        and event["values"].get("amount", 0) > 0
    ]
    checkpoint_route_unknown = request.get("data_list_unavailable") is True
    if request.get("srcChainId") or (checkpoint_route_unknown and recorded_multichain):
        actual = Counter(
            (event["values"].get("token"), event["values"].get("amount"))
            for event in recorded_multichain
            if checkpoint_route_unknown or event["values"].get("srcChainId") == request.get("srcChainId")
        )
        transferred = Counter(
            (event["token"], event["amount"])
            for event in payout_events
            if event["event_name"] == "ERC20Transfer" and event["amount"] > 0
            and event["to"] == ARBITRUM_MULTICHAIN_VAULT
        )
    else:
        receiver = request.get("receiver")
        wnt = metadata.get("tokens", {}).get("index", {}).get("address", "").lower()
        actual = Counter(
            (event["token"], event["amount"])
            for event in payout_events
            if event["event_name"] == "ERC20Transfer" and event["amount"] > 0
            and (event["to"] == receiver or (
                request.get("shouldUnwrapNativeToken") is True and event["token"] == wnt
                and event["to"] == "0x" + "0" * 40 and event["from"] != ARBITRUM_ORDER_VAULT
            ))
        )
        transferred = actual
    payout_matches = actual == expected and transferred == expected
    checks["output_amounts"] = _comparison(
        model["swaps_valid"] and payout_matches, "decrease_output_amount_mismatch", mismatches,
    )
    cap_matches = values.get("basePnlUsd") == values.get("uncappedBasePnlUsd")
    if cap_matches:
        checks["net_realized_pnl"] = _comparison(
            checks.get("uncapped_pnl") == "matched" and collateral_matches and payout_matches,
            "net_realized_pnl_mismatch", mismatches,
        )
    else:
        checks["net_realized_pnl"] = "unavailable"
    minimum = request.get("minOutputAmount", 0)
    if isinstance(minimum, int) and minimum > 0:
        prices = model["oracle_prices"]
        primary_price = prices.get(model["output_token"], {}).get("minPrice")
        secondary_price = prices.get(model["secondary_output_token"], {}).get("minPrice")
        if not isinstance(primary_price, int) or (model["secondary_output_amount"] > 0 and not isinstance(secondary_price, int)):
            checks["minimum_output_usd"] = "unavailable"
        else:
            output_usd = model["output_amount"] * primary_price + model["secondary_output_amount"] * (secondary_price or 0)
            checks["minimum_output_usd"] = _comparison(output_usd >= minimum, "minimum_output_usd_mismatch", mismatches)
    result = {key: value for key, value in model.items() if key not in {"oracle_prices", "collateral_oracle"}}
    result["payout_route"] = "checkpoint_route_inferred_from_receipt" if checkpoint_route_unknown and recorded_multichain else (
        "multichain" if request.get("srcChainId") else "direct"
    )
    return result


def _compare_execution(
    final_request: dict[str, Any], position: dict[str, Any], checks: dict[str, str], mismatches: list[str]
) -> None:
    values = position["values"]
    requested_size = final_request.get("sizeDeltaUsd")
    if requested_size == MAX_UINT256:
        checks["size_delta"] = "matched_full_position_close"
    else:
        checks["size_delta"] = _comparison(
            values.get("sizeDeltaUsd") == requested_size, "size_delta_mismatch", mismatches
        )
    acceptable = final_request.get("acceptablePrice")
    execution_price = values.get("executionPrice")
    is_long = final_request.get("isLong")
    if acceptable in (MAX_UINT256, 0):
        checks["acceptable_price"] = "not_applicable"
        return
    if not isinstance(acceptable, int) or not isinstance(execution_price, int):
        checks["acceptable_price"] = "unavailable"
        return
    is_increase = position["event_name"] == "PositionIncrease"
    buying_index = bool(is_long) == is_increase
    accepted = execution_price <= acceptable if buying_index else execution_price >= acceptable
    checks["acceptable_price"] = _comparison(accepted, "acceptable_price_violation", mismatches)


def _compare_position_math(position: dict[str, Any], checks: dict[str, str], mismatches: list[str]) -> None:
    """Calculate position size and uncapped PnL from pre-execution state."""
    previous = position.get("pre_position")
    values = position["values"]
    oracle = position.get("oracle_at_event")
    if isinstance(oracle, dict):
        checks["oracle_range"] = (
            "matched" if values.get("indexTokenPrice.min") == oracle.get("minPrice")
            and values.get("indexTokenPrice.max") == oracle.get("maxPrice")
            else "different_from_oracle_update"
        )
    else:
        checks["oracle_range"] = "unavailable"
    if not isinstance(previous, dict):
        if position["event_name"] == "PositionIncrease":
            previous = {"sizeInUsd": 0, "sizeInTokens": 0, "collateralAmount": 0}
        else:
            checks["pre_position"] = "unavailable"
            return
    old_usd = previous.get("sizeInUsd")
    old_tokens = previous.get("sizeInTokens")
    delta_usd = values.get("sizeDeltaUsd")
    delta_tokens = values.get("sizeDeltaInTokens")
    if not all(isinstance(value, int) for value in (old_usd, old_tokens, delta_usd, delta_tokens)):
        checks["pre_position"] = "unavailable"
        return
    increase = position["event_name"] == "PositionIncrease"
    sign = 1 if increase else -1
    checks["position_size_usd"] = _comparison(
        values.get("sizeInUsd") == old_usd + sign * delta_usd,
        "position_size_usd_mismatch", mismatches,
    )
    checks["position_size_tokens"] = _comparison(
        values.get("sizeInTokens") == old_tokens + sign * delta_tokens,
        "position_size_tokens_mismatch", mismatches,
    )
    _compare_execution_price(values, old_usd, old_tokens, checks, mismatches, increase=increase)
    if increase or old_usd <= 0 or old_tokens <= 0:
        return
    expected_tokens = old_tokens if delta_usd == old_usd else (
        _ceil_div(old_tokens * delta_usd, old_usd) if values.get("isLong")
        else old_tokens * delta_usd // old_usd
    )
    checks["decrease_tokens"] = _comparison(
        delta_tokens == expected_tokens, "decrease_tokens_mismatch", mismatches
    )
    price = values.get("indexTokenPrice.min" if values.get("isLong") else "indexTokenPrice.max")
    if not isinstance(price, int):
        checks["uncapped_pnl"] = "unavailable"
        return
    total_pnl = old_tokens * price - old_usd
    if not values.get("isLong"):
        total_pnl = -total_pnl
    expected_pnl = _trunc_div(total_pnl * expected_tokens, old_tokens)
    checks["uncapped_pnl"] = _comparison(
        values.get("uncappedBasePnlUsd") == expected_pnl,
        "uncapped_pnl_mismatch", mismatches,
    )
    pending_amount = previous.get("pendingImpactAmount")
    if isinstance(pending_amount, int) and old_usd > 0:
        proportional_amount = _proportional_pending_impact(pending_amount, delta_usd, old_usd)
        impact_price = values.get("indexTokenPrice.min" if proportional_amount > 0 else "indexTokenPrice.max")
        if isinstance(impact_price, int):
            checks["proportional_pending_impact"] = _comparison(
                values.get("proportionalPendingImpactUsd") == proportional_amount * impact_price,
                "proportional_pending_impact_mismatch", mismatches,
            )


def _compare_execution_price(
    values: dict[str, Any], old_usd: int, old_tokens: int,
    checks: dict[str, str], mismatches: list[str], *, increase: bool
) -> None:
    """Calculate price from size and observed impact, with integer truncation."""
    size = values.get("sizeDeltaUsd")
    tokens = values.get("sizeDeltaInTokens")
    is_long = values.get("isLong")
    oracle_price = values.get("indexTokenPrice.max" if increase == bool(is_long) else "indexTokenPrice.min")
    if not all(isinstance(value, int) for value in (size, tokens, oracle_price)):
        checks["execution_price"] = "unavailable"
        return
    if size == 0:
        expected = oracle_price
    elif increase:
        pending_impact = values.get("pendingPriceImpactAmount")
        if not isinstance(pending_impact, int):
            checks["execution_price"] = "unavailable"
            return
        effective_tokens = tokens + (pending_impact if is_long else -pending_impact)
        if effective_tokens <= 0:
            checks["execution_price"] = "unavailable"
            return
        expected = size // effective_tokens
    else:
        impact = values.get("priceImpactUsd")
        if not isinstance(impact, int) or old_tokens <= 0:
            checks["execution_price"] = "unavailable"
            return
        adjustment = _trunc_div(_trunc_div(old_usd * (impact if is_long else -impact), old_tokens), size)
        expected = oracle_price + adjustment
    checks["execution_price"] = _comparison(
        values.get("executionPrice") == expected,
        "execution_price_mismatch", mismatches,
    )


def _compare_fee_math(
    request: dict[str, Any], position: dict[str, Any], fees: dict[str, Any],
    checks: dict[str, str], mismatches: list[str]
) -> None:
    """Calculate fees using the recorded factor and the pre-execution position."""
    values = fees["values"]
    position_values = position["values"]
    price = values.get("collateralTokenPrice.min")
    size = position_values.get("sizeDeltaUsd")
    factor = values.get("positionFeeFactor")
    if not isinstance(price, int) or price <= 0 or not isinstance(size, int) or size < 0 or not isinstance(factor, int):
        checks["position_fee"] = "unavailable"
        return
    expected_position_fee = (size * factor // FLOAT_PRECISION) // price
    checks["position_fee"] = _comparison(
        values.get("positionFeeAmount") == expected_position_fee,
        "position_fee_mismatch", mismatches,
    )
    previous = position.get("pre_position")
    if not isinstance(previous, dict) and position["event_name"] == "PositionIncrease":
        previous = {"sizeInUsd": 0, "collateralAmount": 0, "borrowingFactor": 0, "fundingFeeAmountPerSize": 0}
    if request.get("orderType") == 7:
        # Liquidation uses a separate settlement path. Its fee event may contain
        # zeroes even when the ordinary position-fee formula would be positive.
        checks["liquidation_settlement"] = "unavailable"
        return
    if isinstance(previous, dict) and isinstance(previous.get("sizeInUsd"), int):
        old_size = previous["sizeInUsd"]
        old_factor = previous.get("borrowingFactor")
        new_factor = fees.get("cumulative_borrowing_factor")
        if isinstance(old_factor, int) and isinstance(new_factor, int) and new_factor >= old_factor:
            expected_borrowing_usd = old_size * (new_factor - old_factor) // FLOAT_PRECISION
            checks["borrowing_fee_usd"] = _comparison(
                values.get("borrowingFeeUsd") == expected_borrowing_usd,
                "borrowing_fee_usd_mismatch", mismatches,
            )
            checks["borrowing_fee_amount"] = _comparison(
                values.get("borrowingFeeAmount") == expected_borrowing_usd // price,
                "borrowing_fee_amount_mismatch", mismatches,
            )
        old_funding = previous.get("fundingFeeAmountPerSize")
        latest_funding = values.get("latestFundingFeeAmountPerSize")
        if isinstance(old_funding, int) and isinstance(latest_funding, int) and latest_funding >= old_funding:
            expected_funding = _ceil_div(old_size * (latest_funding - old_funding), FUNDING_PRECISION)
            checks["funding_fee"] = _comparison(
                values.get("fundingFeeAmount") == expected_funding,
                "funding_fee_mismatch", mismatches,
            )
        for token_side in ("Long", "Short"):
            old_claimable = previous.get(f"{token_side.lower()}TokenClaimableFundingAmountPerSize")
            latest_claimable = values.get(f"latest{token_side}TokenClaimableFundingAmountPerSize")
            if isinstance(old_claimable, int) and isinstance(latest_claimable, int) and latest_claimable >= old_claimable:
                checks[f"claimable_{token_side.lower()}_funding"] = _comparison(
                    values.get(f"claimable{token_side}TokenAmount") == old_size * (latest_claimable - old_claimable) // FUNDING_PRECISION,
                    f"claimable_{token_side.lower()}_funding_mismatch", mismatches,
                )
    else:
        checks["pre_position_fees"] = "unavailable"
    amount = values.get("positionFeeAmount")
    borrowing = values.get("borrowingFeeAmount")
    funding = values.get("fundingFeeAmount")
    liquidation = values.get("liquidationFeeAmount", 0)
    ui = values.get("uiFeeAmount")
    discount = max(values.get("referral.traderDiscountAmount", 0), values.get("pro.traderDiscountAmount", 0))
    if all(isinstance(value, int) for value in (amount, borrowing, funding, liquidation, ui, discount)):
        checks["total_cost"] = _comparison(
            values.get("totalCostAmount") == amount + borrowing + funding + liquidation + ui - discount,
            "total_cost_mismatch", mismatches,
        )
    if position["event_name"] == "PositionIncrease" and isinstance(previous, dict):
        input_collateral = request.get("initialCollateralDeltaAmount")
        if isinstance(input_collateral, int) and position_values.get("collateralToken") == request.get("initialCollateralToken"):
            checks["increase_collateral"] = _comparison(
                position_values.get("collateralAmount") == previous.get("collateralAmount", 0) + input_collateral - values.get("totalCostAmount", 0),
                "increase_collateral_mismatch", mismatches,
            )


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _proportional_pending_impact(amount: int, delta_size: int, total_size: int) -> int:
    numerator = amount * delta_size
    return -_ceil_div(-numerator, total_size) if numerator < 0 else numerator // total_size


def _trunc_div(numerator: int, denominator: int) -> int:
    return (1 if numerator >= 0 else -1) * (abs(numerator) // denominator)


def _comparison(passed: bool, mismatch: str, mismatches: list[str]) -> str:
    if passed:
        return "matched"
    mismatches.append(mismatch)
    return "mismatch"


def _order_result(
    key: str,
    request: dict[str, Any],
    final_request: dict[str, Any],
    terminal: dict[str, Any] | None,
    observed: list[dict[str, Any]],
    checks: dict[str, str],
    mismatches: list[str],
    status: str,
) -> dict[str, Any]:
    result = {
        "key": key,
        "origin": request.get("source", "created_in_window"),
        "status": status,
        "request": request,
        "final_request": final_request,
        "terminal": terminal,
        "observed_execution_events": observed,
        "checks": checks,
        "mismatches": mismatches,
    }
    if terminal is not None:
        if request.get("source") == "opening_checkpoint":
            result["blocks_since_opening_checkpoint"] = terminal["block_number"] - request["block_number"]
        else:
            result["request_to_terminal_blocks"] = terminal["block_number"] - request["block_number"]
    return result


def _decode_recorded_log(event: dict[str, Any]) -> DecodedEventLog | None:
    try:
        return decode_event_log(event["payload"]["log"]["data"])
    except (EventDecodeError, KeyError):
        return None


def _event_entry(event: dict[str, Any], decoded: DecodedEventLog) -> dict[str, Any]:
    log = event["payload"]["log"]
    return {
        "event_name": decoded.event_name,
        "values": decoded.values,
        "block_number": event["block_number"],
        "transaction_index": event["transaction_index"],
        "log_index": event["log_index"],
        "transaction_hash": log["transactionHash"].lower(),
    }


def _event_entry_from_log(log: dict[str, Any], decoded: DecodedEventLog) -> dict[str, Any]:
    return {
        "event_name": decoded.event_name,
        "values": decoded.values,
        "block_number": int(log["blockNumber"], 16),
        "transaction_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "transaction_hash": log["transactionHash"].lower(),
    }


def _order_key(decoded: DecodedEventLog) -> str | None:
    key = decoded.values.get("key")
    return key.lower() if isinstance(key, str) else None


def _coordinate(entry: dict[str, Any]) -> tuple[int, int, int]:
    return (int(entry["block_number"]), int(entry["transaction_index"]), int(entry["log_index"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate reconstructed GMX order outcomes against observed events.")
    parser.add_argument("recording", type=Path, help="GMX recording directory")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report to this file")
    parser.add_argument("--json", action="store_true", help="Print the complete JSON report")
    args = parser.parse_args()
    report = validate_orders(args.recording)
    result = asdict(report)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Orders created  {report.orders_created}")
        print(f"Opening closed  {report.opening_terminal_orders}")
        print(f"Terminal orders {report.terminal_orders}")
        print(f"Matched         {report.matched}")
        print(f"Mismatched      {report.mismatched}")
        print(f"Unresolved      {report.unresolved}")
        print(f"Decode errors   {report.decode_errors}")
        print(f"Checked fields {'PASS' if report.implemented_checks_pass else 'FAIL'}")
        print(f"Economics       {'COMPLETE' if report.economic_calibration_complete else 'PARTIAL'}")
        print(f"Open checks     {', '.join(report.remaining_economic_checks)}")
        print(f"Validation      {'PASS' if report.complete else 'INCOMPLETE'}")
    return 0 if report.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
