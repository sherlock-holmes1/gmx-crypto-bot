"""Pure integer model of decrease-position collateral settlement."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.domain.entries import _coordinate
from gmx_crypto_bot_v2.models.arithmetic import _ceil_div


def _model_decrease_settlement(
    request: dict[str, Any],
    position: dict[str, Any],
    fees: dict[str, Any],
    swap_events: list[dict[str, Any]],
    metadata: dict[str, Any],
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
    if (
        not all(
            isinstance(value, int)
            for value in (
                pnl_min,
                pnl_max,
                collateral_price,
                base,
                impact,
                funding,
                total_fee,
                collateral,
            )
        )
        or min(pnl_min, pnl_max, collateral_price) <= 0
        or total_fee < funding
    ):
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
        swaps_valid &= (
            hop.get("amountIn") == secondary and hop.get("tokenOut") == collateral_token
        )
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

    costs_paid = all(
        (
            pay_cost(funding * collateral_price),
            pay_cost(-base) if base < 0 else True,
            pay_cost((total_fee - funding) * collateral_price),
            pay_cost(-impact) if impact < 0 else True,
            pay_cost(values.get("values.priceImpactDiffUsd", 0)),
        )
    )
    requested_withdrawal = request.get("initialCollateralDeltaAmount")
    old_size = previous.get("sizeInUsd")
    size_delta = values.get("sizeDeltaUsd")
    if (
        not isinstance(requested_withdrawal, int)
        or not isinstance(old_size, int)
        or not isinstance(size_delta, int)
    ):
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
        if (
            hop.get("tokenIn") == collateral_token
            and hop.get("amountIn") == output
            and hop.get("tokenOut") == pnl_token
            and hop.get("receiver") == request.get("market")
        ):
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
