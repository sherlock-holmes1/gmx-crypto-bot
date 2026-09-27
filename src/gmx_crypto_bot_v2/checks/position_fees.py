"""Position fee arithmetic and request-to-terminal execution-fee balance checks."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.checks.common import _comparison
from gmx_crypto_bot_v2.domain.constants import FLOAT_PRECISION, FUNDING_PRECISION
from gmx_crypto_bot_v2.models.arithmetic import _ceil_div


def _compare_execution_fee_events(
    request: dict[str, Any],
    events: list[dict[str, Any]],
    checks: dict[str, str],
    mismatches: list[str],
    *,
    updates: list[dict[str, Any]] | None = None,
    update_topups: dict[tuple[str, int], int] | None = None,
) -> None:
    fee = request.get("executionFee")
    keeper = [entry for entry in events if entry["event_name"] == "KeeperExecutionFee"]
    refund = [
        entry
        for entry in events
        if entry["event_name"] in {"ExecutionFeeRefund", "ExecutionFeeRefundCallback"}
    ]
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
    update_entries = [
        entry for entry in updates or [] if entry["event_name"] == "OrderUpdated"
    ]
    topups = update_topups or {}
    if any(
        (entry["transaction_hash"], entry["log_index"]) not in topups
        for entry in update_entries
    ):
        checks["execution_fee_event_balance"] = "unavailable_update_receipt"
        return
    fee += sum(
        topups[(entry["transaction_hash"], entry["log_index"])]
        for entry in update_entries
    )
    if update_entries and paid != fee:
        checks["execution_fee_event_balance"] = "unavailable_update_receipt"
        return
    checks["execution_fee_event_balance"] = _comparison(
        keeper_amount >= 0 and refund_amount >= 0 and paid == fee,
        "execution_fee_event_balance_mismatch",
        mismatches,
    )


def _compare_fee_math(
    request: dict[str, Any],
    position: dict[str, Any],
    fees: dict[str, Any],
    checks: dict[str, str],
    mismatches: list[str],
) -> None:
    """Calculate fees using the recorded factor and the pre-execution position."""
    values = fees["values"]
    position_values = position["values"]
    price = values.get("collateralTokenPrice.min")
    size = position_values.get("sizeDeltaUsd")
    factor = values.get("positionFeeFactor")
    if (
        not isinstance(price, int)
        or price <= 0
        or not isinstance(size, int)
        or size < 0
        or not isinstance(factor, int)
    ):
        checks["position_fee"] = "unavailable"
        return
    expected_position_fee = (size * factor // FLOAT_PRECISION) // price
    checks["position_fee"] = _comparison(
        values.get("positionFeeAmount") == expected_position_fee,
        "position_fee_mismatch",
        mismatches,
    )
    previous = position.get("pre_position")
    if not isinstance(previous, dict) and position["event_name"] == "PositionIncrease":
        previous = {
            "sizeInUsd": 0,
            "collateralAmount": 0,
            "borrowingFactor": 0,
            "fundingFeeAmountPerSize": 0,
        }
    if request.get("orderType") == 7:
        # Liquidation uses a separate settlement path. Its fee event may contain
        # zeroes even when the ordinary position-fee formula would be positive.
        checks["liquidation_settlement"] = "unavailable"
        return
    if isinstance(previous, dict) and isinstance(previous.get("sizeInUsd"), int):
        old_size = previous["sizeInUsd"]
        old_factor = previous.get("borrowingFactor")
        new_factor = fees.get("cumulative_borrowing_factor")
        if (
            isinstance(old_factor, int)
            and isinstance(new_factor, int)
            and new_factor >= old_factor
        ):
            expected_borrowing_usd = (
                old_size * (new_factor - old_factor) // FLOAT_PRECISION
            )
            checks["borrowing_fee_usd"] = _comparison(
                values.get("borrowingFeeUsd") == expected_borrowing_usd,
                "borrowing_fee_usd_mismatch",
                mismatches,
            )
            checks["borrowing_fee_amount"] = _comparison(
                values.get("borrowingFeeAmount") == expected_borrowing_usd // price,
                "borrowing_fee_amount_mismatch",
                mismatches,
            )
        old_funding = previous.get("fundingFeeAmountPerSize")
        latest_funding = values.get("latestFundingFeeAmountPerSize")
        if (
            isinstance(old_funding, int)
            and isinstance(latest_funding, int)
            and latest_funding >= old_funding
        ):
            expected_funding = _ceil_div(
                old_size * (latest_funding - old_funding), FUNDING_PRECISION
            )
            checks["funding_fee"] = _comparison(
                values.get("fundingFeeAmount") == expected_funding,
                "funding_fee_mismatch",
                mismatches,
            )
        for token_side in ("Long", "Short"):
            old_claimable = previous.get(
                f"{token_side.lower()}TokenClaimableFundingAmountPerSize"
            )
            latest_claimable = values.get(
                f"latest{token_side}TokenClaimableFundingAmountPerSize"
            )
            if (
                isinstance(old_claimable, int)
                and isinstance(latest_claimable, int)
                and latest_claimable >= old_claimable
            ):
                checks[f"claimable_{token_side.lower()}_funding"] = _comparison(
                    values.get(f"claimable{token_side}TokenAmount")
                    == old_size
                    * (latest_claimable - old_claimable)
                    // FUNDING_PRECISION,
                    f"claimable_{token_side.lower()}_funding_mismatch",
                    mismatches,
                )
    else:
        checks["pre_position_fees"] = "unavailable"
    amount = values.get("positionFeeAmount")
    borrowing = values.get("borrowingFeeAmount")
    funding = values.get("fundingFeeAmount")
    liquidation = values.get("liquidationFeeAmount", 0)
    ui = values.get("uiFeeAmount")
    discount = max(
        values.get("referral.traderDiscountAmount", 0),
        values.get("pro.traderDiscountAmount", 0),
    )
    if all(
        isinstance(value, int)
        for value in (amount, borrowing, funding, liquidation, ui, discount)
    ):
        checks["total_cost"] = _comparison(
            values.get("totalCostAmount")
            == amount + borrowing + funding + liquidation + ui - discount,
            "total_cost_mismatch",
            mismatches,
        )
    if position["event_name"] == "PositionIncrease" and isinstance(previous, dict):
        input_collateral = request.get("initialCollateralDeltaAmount")
        if isinstance(input_collateral, int) and position_values.get(
            "collateralToken"
        ) == request.get("initialCollateralToken"):
            checks["increase_collateral"] = _comparison(
                position_values.get("collateralAmount")
                == previous.get("collateralAmount", 0)
                + input_collateral
                - values.get("totalCostAmount", 0),
                "increase_collateral_mismatch",
                mismatches,
            )
