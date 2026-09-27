"""Pure GMX position-impact curve and cap inputs."""

from __future__ import annotations

from decimal import Decimal, localcontext

FLOAT_PRECISION = 10**30


def balance_impact(
    long_interest: int,
    short_interest: int,
    signed_delta: int,
    is_long: bool,
    positive_factor: int,
    negative_factor: int,
    positive_exponent: int,
    negative_exponent: int,
    apply_exponent,
) -> tuple[int, bool]:
    """Raw OI-balance impact, before virtual-inventory and pool caps."""
    next_side = max(0, (long_interest if is_long else short_interest) + signed_delta)
    next_long, next_short = (
        (next_side, short_interest) if is_long else (long_interest, next_side)
    )
    initial_diff, next_diff = (
        abs(long_interest - short_interest),
        abs(next_long - next_short),
    )
    improved = next_diff < initial_diff
    positive_factor = min(positive_factor, negative_factor)
    positive_exponent = min(positive_exponent, negative_exponent)

    def adjusted(diff: int, factor: int, exponent: int) -> int:
        return apply_exponent(diff, exponent) * factor // FLOAT_PRECISION

    same_side = (long_interest <= short_interest) == (next_long <= next_short)
    if same_side:
        factor = positive_factor if improved else negative_factor
        exponent = positive_exponent if improved else negative_exponent
        initial, final = (
            adjusted(initial_diff, factor, exponent),
            adjusted(next_diff, factor, exponent),
        )
        return (abs(initial - final) if improved else -abs(initial - final)), improved
    initial = adjusted(initial_diff, positive_factor, positive_exponent)
    final = adjusted(next_diff, negative_factor, negative_exponent)
    return initial - final, improved


def apply_exponent_factor(value: int, exponent_factor: int) -> int:
    """GMX 30-decimal to 18-decimal pow bridge, with Decimal approximation.

    PRBMathUD60x18's final few wei can differ; the validator uses a fixed
    USD-integer tolerance for this comparison, never exact equality.
    """
    if value < FLOAT_PRECISION:
        return 0
    if exponent_factor == FLOAT_PRECISION:
        return value
    with localcontext() as context:
        context.prec = 80
        base = Decimal(value // 10**12) / 10**18
        exponent = Decimal(exponent_factor // 10**12) / 10**18
        return int(base**exponent * 10**18) * 10**12


def predict_current_impact(
    long_interest_tokens: int,
    short_interest_tokens: int,
    virtual_inventory_tokens: int,
    token_delta: int,
    is_long: bool,
    index_min: int,
    index_max: int,
    factors: dict[str, int],
) -> dict[str, int | bool]:
    """Calculate GMX's OI and virtual-inventory curves before settlement caps."""
    mid = (index_min + index_max) // 2
    signed_usd_delta = token_delta * mid
    args = (
        factors["position_impact_factor_positive"],
        factors["position_impact_factor_negative"],
        factors["position_impact_exponent_factor_positive"],
        factors["position_impact_exponent_factor_negative"],
        apply_exponent_factor,
    )
    market_impact, improved = balance_impact(
        long_interest_tokens * mid,
        short_interest_tokens * mid,
        signed_usd_delta,
        is_long,
        *args,
    )
    virtual_impact = market_impact
    if market_impact < 0:
        virtual_usd = virtual_inventory_tokens * mid
        virtual_long = max(-virtual_usd, 0)
        virtual_short = max(virtual_usd, 0)
        if signed_usd_delta < 0:
            virtual_long -= signed_usd_delta
            virtual_short -= signed_usd_delta
        virtual_impact, virtual_improved = balance_impact(
            virtual_long,
            virtual_short,
            signed_usd_delta,
            is_long,
            *args,
        )
        if virtual_impact < market_impact:
            improved = virtual_improved
    selected = min(market_impact, virtual_impact)
    return {
        "market_impact_usd": market_impact,
        "virtual_impact_usd": virtual_impact,
        "selected_impact_usd": selected,
        "balance_was_improved": improved,
    }
