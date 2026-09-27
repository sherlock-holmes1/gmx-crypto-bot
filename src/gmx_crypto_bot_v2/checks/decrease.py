"""Compare collateral conversion and decrease settlement outcomes."""

from __future__ import annotations

from collections import Counter
from typing import Any

from gmx_crypto_bot_v2.checks.common import _comparison
from gmx_crypto_bot_v2.domain.constants import (
    ARBITRUM_MULTICHAIN_VAULT,
    ARBITRUM_ORDER_VAULT,
)
from gmx_crypto_bot_v2.models.decrease import _model_decrease_settlement


def _compare_collateral_conversion(
    request: dict[str, Any],
    position: dict[str, Any],
    fees: dict[str, Any],
    swap_events: list[dict[str, Any]],
    checks: dict[str, str],
    mismatches: list[str],
) -> None:
    """Reconcile the recorded swap path and recompute each hop's token output."""
    hops = [event for event in swap_events if event["event_name"] == "SwapInfo"]
    output_token = position["values"].get("collateralToken")
    if not hops:
        checks["collateral_conversion"] = (
            "not_applicable"
            if output_token == request.get("initialCollateralToken")
            else "unavailable"
        )
        return
    chain_valid = True
    arithmetic_valid = True
    token = request.get("initialCollateralToken")
    amount = request.get("initialCollateralDeltaAmount")
    for hop in hops:
        values = hop["values"]
        chain_valid &= (
            values.get("tokenIn") == token and values.get("amountIn") == amount
        )
        token, amount = values.get("tokenOut"), values.get("amountOut")
        after_fees = values.get("amountInAfterFees")
        price_in = values.get("tokenInPrice")
        price_out = values.get("tokenOutPrice")
        impact = values.get("priceImpactAmount")
        input_impact = values.get("tokenInPriceImpactAmount")
        if (
            not all(
                isinstance(value, int)
                for value in (
                    after_fees,
                    price_in,
                    price_out,
                    impact,
                    input_impact,
                    amount,
                )
            )
            or price_out <= 0
        ):
            arithmetic_valid = False
            continue
        effective_input = after_fees + input_impact + min(impact, 0)
        arithmetic_valid &= (
            effective_input >= 0
            and amount == effective_input * price_in // price_out + max(impact, 0)
        )
    collateral_delta = position["values"].get("collateralDeltaAmount")
    total_cost = fees["values"].get("totalCostAmount")
    chain_valid &= token == output_token and all(
        isinstance(value, int) for value in (amount, collateral_delta, total_cost)
    )
    if (
        isinstance(amount, int)
        and isinstance(collateral_delta, int)
        and isinstance(total_cost, int)
    ):
        chain_valid &= amount == collateral_delta + total_cost
    minimum = request.get("minOutputAmount")
    if isinstance(minimum, int) and isinstance(amount, int):
        chain_valid &= amount >= minimum
    checks["collateral_conversion"] = _comparison(
        chain_valid, "collateral_conversion_mismatch", mismatches
    )
    checks["swap_output_arithmetic"] = _comparison(
        arithmetic_valid, "swap_output_arithmetic_mismatch", mismatches
    )


def _compare_decrease_settlement(
    request: dict[str, Any],
    position: dict[str, Any],
    fees: dict[str, Any],
    swap_events: list[dict[str, Any]],
    payout_events: list[dict[str, Any]],
    metadata: dict[str, Any],
    checks: dict[str, str],
    mismatches: list[str],
) -> dict[str, Any] | None:
    model = _model_decrease_settlement(request, position, fees, swap_events, metadata)
    if model is None:
        for check in (
            "decrease_collateral_and_cash",
            "net_realized_pnl",
            "output_amounts",
        ):
            checks[check] = "unavailable"
        return None
    values = position["values"]
    previous = position["pre_position"]
    collateral_matches = (
        model["costs_paid"]
        and model["swaps_valid"]
        and values.get("collateralAmount") == model["remaining_collateral"]
        and values.get("collateralDeltaAmount")
        == previous["collateralAmount"] - model["remaining_collateral"]
    )
    checks["decrease_collateral_and_cash"] = _comparison(
        collateral_matches,
        "decrease_collateral_mismatch",
        mismatches,
    )
    expected = Counter()
    if model["output_amount"] > 0:
        expected[(model["output_token"], model["output_amount"])] += 1
    if model["secondary_output_amount"] > 0:
        expected[
            (model["secondary_output_token"], model["secondary_output_amount"])
        ] += 1
    recorded_multichain = [
        event
        for event in payout_events
        if event["event_name"] == "MultichainTransferIn"
        and event["values"].get("account") == request.get("receiver")
        and event["values"].get("amount", 0) > 0
    ]
    checkpoint_route_unknown = request.get("data_list_unavailable") is True
    if request.get("srcChainId") or (checkpoint_route_unknown and recorded_multichain):
        actual = Counter(
            (event["values"].get("token"), event["values"].get("amount"))
            for event in recorded_multichain
            if checkpoint_route_unknown
            or event["values"].get("srcChainId") == request.get("srcChainId")
        )
        transferred = Counter(
            (event["token"], event["amount"])
            for event in payout_events
            if event["event_name"] == "ERC20Transfer"
            and event["amount"] > 0
            and event["to"] == ARBITRUM_MULTICHAIN_VAULT
        )
    else:
        receiver = request.get("receiver")
        wnt = metadata.get("tokens", {}).get("index", {}).get("address", "").lower()
        actual = Counter(
            (event["token"], event["amount"])
            for event in payout_events
            if event["event_name"] == "ERC20Transfer"
            and event["amount"] > 0
            and (
                event["to"] == receiver
                or (
                    request.get("shouldUnwrapNativeToken") is True
                    and event["token"] == wnt
                    and event["to"] == "0x" + "0" * 40
                    and event["from"] != ARBITRUM_ORDER_VAULT
                )
            )
        )
        transferred = actual
    payout_matches = actual == expected and transferred == expected
    checks["output_amounts"] = _comparison(
        model["swaps_valid"] and payout_matches,
        "decrease_output_amount_mismatch",
        mismatches,
    )
    cap_matches = values.get("basePnlUsd") == values.get("uncappedBasePnlUsd")
    if cap_matches:
        checks["net_realized_pnl"] = _comparison(
            checks.get("uncapped_pnl") == "matched"
            and collateral_matches
            and payout_matches,
            "net_realized_pnl_mismatch",
            mismatches,
        )
    else:
        checks["net_realized_pnl"] = "unavailable"
    minimum = request.get("minOutputAmount", 0)
    if isinstance(minimum, int) and minimum > 0:
        prices = model["oracle_prices"]
        primary_price = prices.get(model["output_token"], {}).get("minPrice")
        secondary_price = prices.get(model["secondary_output_token"], {}).get(
            "minPrice"
        )
        if not isinstance(primary_price, int) or (
            model["secondary_output_amount"] > 0
            and not isinstance(secondary_price, int)
        ):
            checks["minimum_output_usd"] = "unavailable"
        else:
            output_usd = model["output_amount"] * primary_price + model[
                "secondary_output_amount"
            ] * (secondary_price or 0)
            checks["minimum_output_usd"] = _comparison(
                output_usd >= minimum, "minimum_output_usd_mismatch", mismatches
            )
    result = {
        key: value
        for key, value in model.items()
        if key not in {"oracle_prices", "collateral_oracle"}
    }
    result["payout_route"] = (
        "checkpoint_route_inferred_from_receipt"
        if checkpoint_route_unknown and recorded_multichain
        else ("multichain" if request.get("srcChainId") else "direct")
    )
    return result
