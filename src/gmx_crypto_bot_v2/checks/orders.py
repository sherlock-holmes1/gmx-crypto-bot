"""Dependency-ordered checks for one order and its supplied evidence context."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.checks.accrual import (
    compare_accrual,
    compare_liquidation_configuration,
)
from gmx_crypto_bot_v2.checks.common import _comparison
from gmx_crypto_bot_v2.checks.decrease import (
    _compare_collateral_conversion,
    _compare_decrease_settlement,
)
from gmx_crypto_bot_v2.checks.fees import compare_historical_fees
from gmx_crypto_bot_v2.checks.liquidation import compare_liquidation_settlement
from gmx_crypto_bot_v2.checks.position import (
    _compare_execution,
    _compare_independent_price_impact,
    _compare_position_math,
)
from gmx_crypto_bot_v2.checks.position_fees import (
    _compare_execution_fee_events,
    _compare_fee_math,
)
from gmx_crypto_bot_v2.checks.referral import compare_referral
from gmx_crypto_bot_v2.checks.swaps import compare_swaps
from gmx_crypto_bot_v2.domain.constants import (
    DECREASE_ORDER_TYPES,
    INCREASE_ORDER_TYPES,
    TERMINAL_EVENTS,
)
from gmx_crypto_bot_v2.domain.entries import _coordinate
from gmx_crypto_bot_v2.domain.evidence import OrderContext
from gmx_crypto_bot_v2.reconstruction.context import ValidationContext
from gmx_crypto_bot_v2.reporting.orders import _order_result


def validate_order(order: OrderContext, context: ValidationContext) -> dict[str, Any]:
    """Evaluate one order using supplied observations and independent model state."""
    key = order.key
    request = order.request
    lifecycle = order.lifecycle
    observed = order.observed
    receipts = context.receipts
    execution_fees = context.execution_fees
    update_topups = context.update_topups
    opening_positions = context.opening_positions
    position_events = context.position_events
    swap_events = context.swap_events
    payout_events = context.payout_events
    metadata = context.metadata
    impact_factors = context.impact_factors
    fee_histories = context.fee_histories
    referral_state = context.referral_state
    modeled_swaps = context.modeled_swaps
    accrual_replay = context.accrual_replay
    recording = context.traces
    payout_receipt_available = context.payout_receipt_available
    updates = [
        entry for entry in lifecycle if entry["event_name"] not in TERMINAL_EVENTS
    ]
    terminals = [entry for entry in lifecycle if entry["event_name"] in TERMINAL_EVENTS]
    final_request = dict(request["values"])
    for update in updates:
        if update["event_name"] == "OrderSizeDeltaAutoUpdated":
            final_request["sizeDeltaUsd"] = update["values"]["nextSizeDeltaUsd"]
        elif update["event_name"] == "OrderCollateralDeltaAmountAutoUpdated":
            final_request["initialCollateralDeltaAmount"] = update["values"][
                "nextCollateralDeltaAmount"
            ]
        else:
            final_request.update(update["values"])
    mismatches: list[str] = []
    checks: dict[str, str] = {}
    modeled_settlement: dict[str, Any] | None = None
    modeled_price_impact: dict[str, Any] | None = None
    impact_factors_at_execution: dict[str, int | None] | None = None
    historical_fee_factors = None
    modeled_referral = None
    modeled_liquidation_configuration = None
    if not terminals:
        return _order_result(
            key,
            request,
            final_request,
            None,
            observed,
            checks,
            ["no_terminal_event_in_window"],
            "unresolved",
        )
    terminal = terminals[-1]
    fee_events = (execution_fees or {}).get(
        (terminal["transaction_hash"], terminal["log_index"]), []
    )
    _compare_execution_fee_events(
        final_request,
        fee_events,
        checks,
        mismatches,
        updates=updates,
        update_topups=update_topups or {},
    )
    if len(terminals) != 1:
        mismatches.append("multiple_terminal_events")
    checks["terminal_after_request"] = _comparison(
        _coordinate(terminal) > _coordinate(request),
        "terminal_before_request",
        mismatches,
    )
    checks["account"] = _comparison(
        terminal["values"].get("account") == final_request.get("account"),
        "terminal_account_mismatch",
        mismatches,
    )
    if terminal["event_name"] in {"OrderCancelled", "OrderFrozen"}:
        reason = terminal["values"].get("reason")
        reason_bytes = terminal["values"].get("reasonBytes")
        checks["terminal_reason"] = _comparison(
            (isinstance(reason, str) and bool(reason))
            or (isinstance(reason_bytes, str) and reason_bytes not in {"", "0x"}),
            "missing_terminal_reason",
            mismatches,
        )
        checks["terminal_reason_reconstruction"] = _reconstruct_terminal_reason(
            final_request,
            terminal,
            fee_events,
            opening_positions,
            position_events,
            mismatches,
        )
    if terminal["event_name"] == "OrderExecuted":
        related = observed
        position = next(
            (
                entry
                for entry in related
                if entry["event_name"] in {"PositionIncrease", "PositionDecrease"}
            ),
            None,
        )
        fees = next(
            (
                entry
                for entry in related
                if entry["event_name"] == "PositionFeesCollected"
            ),
            None,
        )
        if position is None:
            mismatches.append("missing_position_execution_event")
            checks["position_execution"] = "mismatch"
        else:
            checks["position_execution"] = "matched"
            coordinate = _coordinate(position)[:3]
            impact_factors_at_execution = {
                field: history.at(coordinate)
                for field, history in (impact_factors or {}).items()
            }
            modeled_price_impact = _compare_independent_price_impact(
                position,
                impact_factors_at_execution,
                checks,
                mismatches,
            )
            _compare_execution(final_request, position, checks, mismatches)
            checks["position_transaction"] = _comparison(
                position["transaction_hash"] == terminal["transaction_hash"],
                "position_transaction_mismatch",
                mismatches,
            )
            expected_increase = final_request.get("orderType") in INCREASE_ORDER_TYPES
            expected_decrease = final_request.get("orderType") in DECREASE_ORDER_TYPES
            checks["order_type"] = _comparison(
                (expected_increase and position["event_name"] == "PositionIncrease")
                or (expected_decrease and position["event_name"] == "PositionDecrease"),
                "order_type_mismatch",
                mismatches,
            )
            checks["direction"] = _comparison(
                position["values"].get("isLong") == final_request.get("isLong"),
                "direction_mismatch",
                mismatches,
            )
            checks["collateral_token"] = (
                "matched"
                if position["values"].get("collateralToken")
                == final_request.get("initialCollateralToken")
                else "different_from_input"
            )
            _compare_position_math(position, checks, mismatches)
        if fees is not None and position is not None:
            checks["fee_collateral_token"] = _comparison(
                fees["values"].get("collateralToken")
                == position["values"].get("collateralToken"),
                "fee_collateral_token_mismatch",
                mismatches,
            )
            checks["fee_transaction"] = _comparison(
                fees["transaction_hash"] == terminal["transaction_hash"],
                "fee_transaction_mismatch",
                mismatches,
            )
            checks["fee_trade_size"] = _comparison(
                fees["values"].get("tradeSizeUsd")
                == position["values"].get("sizeDeltaUsd"),
                "fee_trade_size_mismatch",
                mismatches,
            )
            historical_fee_factors = compare_historical_fees(
                final_request,
                position,
                fees,
                modeled_price_impact,
                fee_histories or {},
                checks,
                mismatches,
            )
            compare_accrual(accrual_replay, key, checks, mismatches)
            modeled_liquidation_configuration = compare_liquidation_configuration(
                accrual_replay,
                key,
                final_request,
                position,
                fees,
                modeled_price_impact,
                fee_histories or {},
                checks,
                mismatches,
                referral_state,
                swap_events or [],
            )
            modeled_referral = compare_referral(
                referral_state,
                final_request,
                position,
                fees,
                historical_fee_factors,
                modeled_price_impact,
                checks,
                mismatches,
            )
            _compare_fee_math(final_request, position, fees, checks, mismatches)
            if position["event_name"] == "PositionIncrease":
                _compare_collateral_conversion(
                    final_request, position, fees, swap_events or [], checks, mismatches
                )
            elif final_request.get("orderType") != 7:
                modeled_settlement = _compare_decrease_settlement(
                    final_request,
                    position,
                    fees,
                    swap_events or [],
                    payout_events or [],
                    metadata or {},
                    checks,
                    mismatches,
                )
        elif fees is None:
            checks["position_fees"] = "unavailable"
    receipt = receipts.get(terminal["transaction_hash"])
    if receipt is None:
        checks["receipt"] = "unavailable"
    else:
        checks["receipt"] = _comparison(
            receipt["payload"].get("status") == "0x1",
            "failed_terminal_receipt",
            mismatches,
        )
        gas_used = receipt["payload"].get("gas_used")
        checks["gas_used"] = _comparison(
            isinstance(gas_used, str)
            and gas_used.startswith("0x")
            and all(character in "0123456789abcdefABCDEF" for character in gas_used[2:])
            and int(gas_used, 16) > 0,
            "missing_terminal_gas",
            mismatches,
        )
    compare_swaps(
        swap_events or [], modeled_swaps or [], checks, mismatches, final_request
    )
    liquidation_settlement = None
    if (
        terminal["event_name"] == "OrderExecuted"
        and final_request.get("orderType") == 7
    ):
        checks["liquidation_settlement"] = "unavailable"
        if position is not None and fees is not None:
            liquidation_settlement = compare_liquidation_settlement(
                key,
                final_request,
                position,
                fees,
                swap_events or [],
                payout_events or [],
                metadata or {},
                accrual_replay,
                modeled_price_impact,
                fee_histories or {},
                referral_state,
                checks,
                mismatches,
                recording,
                receipt,
                payout_receipt_available,
            )
    status = "mismatch" if mismatches else "matched"
    result = _order_result(
        key, request, final_request, terminal, observed, checks, mismatches, status
    )
    if liquidation_settlement is not None:
        result["modeled_liquidation_settlement"] = liquidation_settlement
    result["observed_execution_fee_events"] = fee_events
    result["observed_swap_events"] = swap_events or []
    result["modeled_swaps"] = modeled_swaps or []
    result["observed_payout_events"] = payout_events or []
    if modeled_liquidation_configuration is not None:
        result["modeled_liquidation_configuration"] = modeled_liquidation_configuration
    if modeled_referral is not None:
        result["modeled_referral"] = modeled_referral
    if historical_fee_factors is not None:
        result["historical_fee_factors"] = historical_fee_factors
    if impact_factors_at_execution is not None:
        result["historical_price_impact_factors"] = impact_factors_at_execution
    if modeled_price_impact is not None:
        result["modeled_price_impact"] = modeled_price_impact
    if modeled_settlement is not None:
        result["modeled_decrease_settlement"] = modeled_settlement
    result["observed_order_update_topups"] = [
        {
            "transaction_hash": entry["transaction_hash"],
            "log_index": entry["log_index"],
            "amount": (update_topups or {}).get(
                (entry["transaction_hash"], entry["log_index"])
            ),
        }
        for entry in updates
        if entry["event_name"] == "OrderUpdated"
    ]
    return result


def _reconstruct_terminal_reason(
    request: dict[str, Any],
    terminal: dict[str, Any],
    fee_events: list[dict[str, Any]],
    opening_positions: dict[str, dict[str, Any]] | None,
    position_events: list[dict[str, Any]] | None,
    mismatches: list[str],
) -> str:
    """Check the cancellation cause against evidence beyond the reason label."""
    if terminal["event_name"] != "OrderCancelled":
        return "unavailable"
    reason = terminal["values"].get("reason")
    reason_bytes = terminal["values"].get("reasonBytes", "0x")
    if reason == "USER_INITIATED_CANCEL" and reason_bytes == "0x":
        keepers = [
            event["values"].get("keeper")
            for event in fee_events
            if event["event_name"] == "KeeperExecutionFee"
        ]
        if not keepers:
            return "unavailable"
        return _comparison(
            len(keepers) == 1 and keepers[0] == request.get("account"),
            "user_cancel_keeper_mismatch",
            mismatches,
        )
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
        return _comparison(
            request.get("autoCancel") is True
            and request.get("orderType") in {5, 6}
            and closed,
            "auto_cancel_without_matching_position_close",
            mismatches,
        )
    if reason == "" and reason_bytes == "0x4dfbbff3":  # EmptyPosition()
        if opening_positions is None or position_events is None:
            return "unavailable"
        identity = _request_position_identity(request)
        state = next(
            (
                position.get("sizeInUsd")
                for position in opening_positions.values()
                if _position_identity(position) == identity
            ),
            None,
        )
        for event in position_events:
            if _coordinate(event) >= _coordinate(terminal):
                continue
            if event["event_name"] not in {"PositionIncrease", "PositionDecrease"}:
                continue
            if _position_identity(event["values"]) == identity:
                state = event["values"].get("sizeInUsd")
        return _comparison(
            request.get("orderType") == 4 and not state,
            "empty_position_cancel_with_active_position",
            mismatches,
        )
    if reason == "" and isinstance(reason_bytes, str) and len(reason_bytes) == 138:
        selector = reason_bytes[:10]
        left, right = int(reason_bytes[10:74], 16), int(reason_bytes[74:138], 16)
        # GMX Errors.InvalidDecreaseOrderSize(uint256,uint256) and
        # Errors.InvalidPositionSizeValues(uint256,uint256).
        if selector == "0x9fbe2cbc":
            if opening_positions is None or position_events is None:
                return "unavailable"
            identity = _request_position_identity(request)
            state = next(
                (
                    position.get("sizeInUsd")
                    for position in opening_positions.values()
                    if _position_identity(position) == identity
                ),
                None,
            )
            for event in position_events:
                if _coordinate(event) >= _coordinate(terminal):
                    continue
                if (
                    event["event_name"] in {"PositionIncrease", "PositionDecrease"}
                    and _position_identity(event["values"]) == identity
                ):
                    state = event["values"].get("sizeInUsd")
            if state is None:
                return "unavailable"
            return _comparison(
                request.get("orderType") in DECREASE_ORDER_TYPES
                and left == request.get("sizeDeltaUsd")
                and right == state
                and left > right,
                "invalid_decrease_size_reason_mismatch",
                mismatches,
            )
        if selector == "0xbff65b3f":
            return _comparison(
                request.get("orderType") in INCREASE_ORDER_TYPES
                and request.get("sizeDeltaUsd") == 0
                and left == 0
                and right == 0,
                "invalid_position_size_reason_mismatch",
                mismatches,
            )
    if (
        reason == ""
        and isinstance(reason_bytes, str)
        and reason_bytes.startswith("0xe09ad0e9")
    ):
        # OrderNotFulfillableAtAcceptablePrice(uint256,uint256).
        if len(reason_bytes) != 2 + 8 + 64 * 2:
            return "unavailable"
        price = int(reason_bytes[10:74], 16)
        acceptable = int(reason_bytes[74:138], 16)
        buying_index = request.get("isLong") == (
            request.get("orderType") in INCREASE_ORDER_TYPES
        )
        violated = price > acceptable if buying_index else price < acceptable
        return _comparison(
            acceptable == request.get("acceptablePrice") and violated,
            "acceptable_price_cancel_reason_mismatch",
            mismatches,
        )
    return "unavailable"


def _position_identity(values: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (
        values.get("account"),
        values.get("market"),
        values.get("collateralToken"),
        values.get("isLong"),
    )


def _request_position_identity(values: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (
        values.get("account"),
        values.get("market"),
        values.get("initialCollateralToken"),
        values.get("isLong"),
    )
