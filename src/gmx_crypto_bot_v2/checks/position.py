"""Position size, execution price, oracle and independent impact comparisons."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.checks.common import _comparison
from gmx_crypto_bot_v2.domain.constants import (
    FLOAT_PRECISION,
    IMPACT_ROUNDING_TOLERANCE_USD,
    MAX_UINT256,
)
from gmx_crypto_bot_v2.models.arithmetic import (
    _ceil_div,
    _proportional_pending_impact,
    _trunc_div,
)
from gmx_crypto_bot_v2.models.impact import predict_current_impact


def _compare_independent_price_impact(
    position: dict[str, Any],
    factors: dict[str, int | None],
    checks: dict[str, str],
    mismatches: list[str],
) -> dict[str, Any] | None:
    """Compare OI/virtual-inventory impact and decrease caps to chain events."""
    required = (
        "position_impact_factor_positive",
        "position_impact_factor_negative",
        "position_impact_exponent_factor_positive",
        "position_impact_exponent_factor_negative",
        "max_position_impact_factor_positive",
        "max_position_impact_factor_negative",
        "max_lendable_impact_factor",
        "max_lendable_impact_usd",
    )
    values = position["values"]
    interest = position.get("pre_open_interest_tokens")
    virtual = position.get("pre_virtual_inventory_tokens")
    pool = position.get("pre_impact_pool_amount")
    if (
        not isinstance(interest, dict)
        or not isinstance(virtual, int)
        or not isinstance(pool, int)
        or not position.get("open_interest_update_matches")
        or not position.get("virtual_inventory_update_matches")
        or not position.get("virtual_inventory_continuous")
        or not position.get("impact_pool_continuous")
        or any(not isinstance(factors.get(name), int) for name in required)
    ):
        checks["independent_price_impact"] = "unavailable"
        return None
    increase = position["event_name"] == "PositionIncrease"
    size_usd = values.get("sizeDeltaUsd")
    size_tokens = values.get("sizeDeltaInTokens")
    index_min = values.get("indexTokenPrice.min")
    index_max = values.get("indexTokenPrice.max")
    if not all(
        isinstance(value, int)
        for value in (size_usd, size_tokens, index_min, index_max)
    ):
        checks["independent_price_impact"] = "unavailable"
        return None
    token_delta = size_tokens if increase else -size_tokens
    model = predict_current_impact(
        interest["long"],
        interest["short"],
        virtual,
        token_delta,
        values.get("isLong") is True,
        index_min,
        index_max,
        factors,
    )
    current = model["selected_impact_usd"]
    positive_cap = (
        size_usd * factors["max_position_impact_factor_positive"] // FLOAT_PRECISION
    )
    if current > 0:
        current = min(current, positive_cap)
    observed_current = (
        values.get("pendingPriceImpactUsd")
        if increase
        else values.get("priceImpactUsd")
    )
    valid = (
        isinstance(observed_current, int)
        and abs(current - observed_current) <= IMPACT_ROUNDING_TOLERANCE_USD
    )
    model["current_impact_usd"] = current
    if not increase:
        pending = values.get("proportionalPendingImpactUsd")
        if not isinstance(pending, int):
            checks["independent_price_impact"] = "unavailable"
            return None
        total = current + pending
        impact_diff = 0
        if total < 0:
            negative_cap = -(
                size_usd
                * factors["max_position_impact_factor_negative"]
                // FLOAT_PRECISION
            )
            if total < negative_cap:
                impact_diff = negative_cap - total
                total = negative_cap
        if total > 0:
            total = min(total, positive_cap)
            if (
                factors["max_lendable_impact_factor"]
                or factors["max_lendable_impact_usd"]
            ):
                checks["independent_price_impact"] = "unavailable"
                return None
            total = min(total, pool * index_min)
        observed_total = values.get("totalImpactUsd")
        observed_diff = values.get("values.priceImpactDiffUsd", 0)
        valid &= (
            isinstance(observed_total, int)
            and abs(total - observed_total) <= IMPACT_ROUNDING_TOLERANCE_USD
        )
        valid &= (
            isinstance(observed_diff, int)
            and abs(impact_diff - observed_diff) <= IMPACT_ROUNDING_TOLERANCE_USD
        )
        model["total_impact_usd"] = total
        model["negative_cap_diff_usd"] = impact_diff
    checks["independent_price_impact"] = _comparison(
        valid, "independent_price_impact_mismatch", mismatches
    )
    model["rounding_tolerance_usd"] = IMPACT_ROUNDING_TOLERANCE_USD
    return model


def _compare_execution(
    final_request: dict[str, Any],
    position: dict[str, Any],
    checks: dict[str, str],
    mismatches: list[str],
) -> None:
    values = position["values"]
    requested_size = final_request.get("sizeDeltaUsd")
    if requested_size == MAX_UINT256:
        checks["size_delta"] = "matched_full_position_close"
    else:
        checks["size_delta"] = _comparison(
            values.get("sizeDeltaUsd") == requested_size,
            "size_delta_mismatch",
            mismatches,
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
    accepted = (
        execution_price <= acceptable if buying_index else execution_price >= acceptable
    )
    checks["acceptable_price"] = _comparison(
        accepted, "acceptable_price_violation", mismatches
    )


def _compare_position_math(
    position: dict[str, Any], checks: dict[str, str], mismatches: list[str]
) -> None:
    """Calculate position size and uncapped PnL from pre-execution state."""
    previous = position.get("pre_position")
    values = position["values"]
    oracle = position.get("oracle_at_event")
    if isinstance(oracle, dict):
        checks["oracle_range"] = (
            "matched"
            if values.get("indexTokenPrice.min") == oracle.get("minPrice")
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
    if not all(
        isinstance(value, int)
        for value in (old_usd, old_tokens, delta_usd, delta_tokens)
    ):
        checks["pre_position"] = "unavailable"
        return
    increase = position["event_name"] == "PositionIncrease"
    sign = 1 if increase else -1
    checks["position_size_usd"] = _comparison(
        values.get("sizeInUsd") == old_usd + sign * delta_usd,
        "position_size_usd_mismatch",
        mismatches,
    )
    checks["position_size_tokens"] = _comparison(
        values.get("sizeInTokens") == old_tokens + sign * delta_tokens,
        "position_size_tokens_mismatch",
        mismatches,
    )
    _compare_execution_price(
        values, old_usd, old_tokens, checks, mismatches, increase=increase
    )
    if increase or old_usd <= 0 or old_tokens <= 0:
        return
    expected_tokens = (
        old_tokens
        if delta_usd == old_usd
        else (
            _ceil_div(old_tokens * delta_usd, old_usd)
            if values.get("isLong")
            else old_tokens * delta_usd // old_usd
        )
    )
    checks["decrease_tokens"] = _comparison(
        delta_tokens == expected_tokens, "decrease_tokens_mismatch", mismatches
    )
    price = values.get(
        "indexTokenPrice.min" if values.get("isLong") else "indexTokenPrice.max"
    )
    if not isinstance(price, int):
        checks["uncapped_pnl"] = "unavailable"
        return
    total_pnl = old_tokens * price - old_usd
    if not values.get("isLong"):
        total_pnl = -total_pnl
    expected_pnl = _trunc_div(total_pnl * expected_tokens, old_tokens)
    checks["uncapped_pnl"] = _comparison(
        values.get("uncappedBasePnlUsd") == expected_pnl,
        "uncapped_pnl_mismatch",
        mismatches,
    )
    pending_amount = previous.get("pendingImpactAmount")
    if isinstance(pending_amount, int) and old_usd > 0:
        proportional_amount = _proportional_pending_impact(
            pending_amount, delta_usd, old_usd
        )
        impact_price = values.get(
            "indexTokenPrice.min" if proportional_amount > 0 else "indexTokenPrice.max"
        )
        if isinstance(impact_price, int):
            checks["proportional_pending_impact"] = _comparison(
                values.get("proportionalPendingImpactUsd")
                == proportional_amount * impact_price,
                "proportional_pending_impact_mismatch",
                mismatches,
            )


def _compare_execution_price(
    values: dict[str, Any],
    old_usd: int,
    old_tokens: int,
    checks: dict[str, str],
    mismatches: list[str],
    *,
    increase: bool,
) -> None:
    """Calculate price from size and observed impact, with integer truncation."""
    size = values.get("sizeDeltaUsd")
    tokens = values.get("sizeDeltaInTokens")
    is_long = values.get("isLong")
    oracle_price = values.get(
        "indexTokenPrice.max" if increase == bool(is_long) else "indexTokenPrice.min"
    )
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
        adjustment = _trunc_div(
            _trunc_div(old_usd * (impact if is_long else -impact), old_tokens), size
        )
        expected = oracle_price + adjustment
    checks["execution_price"] = _comparison(
        values.get("executionPrice") == expected,
        "execution_price_mismatch",
        mismatches,
    )
