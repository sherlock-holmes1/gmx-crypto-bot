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
UNMODELED_ECONOMICS = (
    "historical_configuration_factors",
    "independent_price_impact",
    "collateral_conversion",
    "decrease_collateral_and_cash",
    "net_realized_pnl",
    "output_amounts",
    "execution_fee_refund",
    "liquidation_settlement",
    "terminal_reason_reconstruction",
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
    created, opening, observed, receipts, decode_errors = _load_replay_evidence(recording, target_market, index_token)
    lifecycle, raw_decode_errors = _load_terminal_lifecycle(recording, set(created) | set(opening))
    decode_errors += raw_decode_errors
    results: list[dict[str, Any]] = []
    mismatch_counts: Counter[str] = Counter()

    for key, request in sorted(created.items(), key=lambda item: _coordinate(item[1])):
        order = _validate_order(key, request, lifecycle.get(key, []), observed.get(key, []), receipts)
        results.append(order)
        mismatch_counts.update(order["mismatches"])
    opening_terminal_orders = 0
    for key, request in sorted(opening.items()):
        if key in created or not any(entry["event_name"] in TERMINAL_EVENTS for entry in lifecycle.get(key, [])):
            continue
        order = _validate_order(key, request, lifecycle[key], observed.get(key, []), receipts)
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
    } | set(UNMODELED_ECONOMICS))
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
    dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], int,
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
                if decoded.values.get("token") == index_token:
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
    _attach_pre_position_state(position_events, opening_positions, borrowing_events, opening_borrowing, oracle_events)
    return created, opening, observed, receipts, decode_errors


def _attach_pre_position_state(
    events: list[dict[str, Any]], opening_positions: dict[str, dict[str, Any]],
    borrowing_events: list[dict[str, Any]], opening_borrowing: dict[str, Any],
    oracle_events: list[dict[str, Any]] | None = None
) -> None:
    """Reconstruct the position immediately before each observed position event."""
    positions = {key.lower(): dict(value) for key, value in opening_positions.items()}
    borrowing = {
        side: opening_borrowing.get(side, {}).get("cumulative_factor", {}).get("nextCumulativeBorrowingFactor")
        for side in ("long", "short")
    }
    oracle_at_transaction: dict[str, Any] | None = None
    oracle_transaction: str | None = None
    for entry in sorted(events + borrowing_events + (oracle_events or []), key=_coordinate):
        values = entry["values"]
        if entry["transaction_hash"] != oracle_transaction:
            oracle_transaction = entry["transaction_hash"]
            oracle_at_transaction = None
        if entry["event_name"] == "OraclePriceUpdate":
            oracle_at_transaction = values
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
        entry["oracle_at_event"] = dict(oracle_at_transaction) if oracle_at_transaction is not None else None
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
        positions[key] = next_position


def _load_terminal_lifecycle(recording: Path, target_keys: set[str]) -> tuple[dict[str, list[dict[str, Any]]], int]:
    lifecycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decode_errors = 0
    for log in _iter_raw_event_emitter_logs(recording):
        name = event_name_from_data(log.get("data", ""))
        if name not in LIFECYCLE_EVENTS:
            continue
        try:
            decoded = decode_event_log(log["data"])
        except (EventDecodeError, KeyError):
            decode_errors += 1
            continue
        key = _order_key(decoded)
        if key in target_keys:
            lifecycle[key].append(_event_entry_from_log(log, decoded))
    for entries in lifecycle.values():
        entries.sort(key=_coordinate)
    return lifecycle, decode_errors


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
    if not terminals:
        return _order_result(key, request, final_request, None, observed, checks, ["no_terminal_event_in_window"], "unresolved")
    terminal = terminals[-1]
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
    return _order_result(key, request, final_request, terminal, observed, checks, mismatches, status)


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
