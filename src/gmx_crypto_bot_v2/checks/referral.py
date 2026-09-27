"""Compare independently reconstructed referral and pro discounts."""

from __future__ import annotations

from gmx_crypto_bot_v2.domain.referral import P
from gmx_crypto_bot_v2.models.referral import discount_amounts
from gmx_crypto_bot_v2.reconstruction.referral import ReferralState


def compare_referral(
    state: ReferralState,
    request: dict,
    position: dict,
    fees: dict,
    fee_factors: dict,
    model: dict | None,
    checks: dict,
    errors: list,
) -> dict | None:
    if request.get("orderType") == 7:
        return None  # separate liquidation settlement gate
    names = (
        "historical_referral_identity",
        "historical_referral_discount",
        "historical_pro_discount",
        "historical_protocol_fee",
    )

    def unavailable():
        for name in names:
            checks[name] = "unavailable"

    coordinate = (fees["block_number"], fees["transaction_index"], fees["log_index"])
    if state is None:
        unavailable()
        return None
    try:
        s = state.at(request["account"].lower(), coordinate)
    except KeyError:
        unavailable()
        return None
    v = fees["values"]
    improved = model.get("balance_was_improved") if model else None
    factor = fee_factors.get(
        "position_fee_positive" if improved else "position_fee_negative"
    )
    price = v.get("collateralTokenPrice.min")
    size = position["values"].get("sizeDeltaUsd")
    if (
        type(improved) is not bool
        or factor is None
        or type(price) is not int
        or price <= 0
        or type(size) is not int
    ):
        unavailable()
        return None
    fee = size * factor // P // price
    expected = discount_amounts(
        fee,
        s["code"],
        s["rebate_bps"],
        s["share_bps"],
        s["minimum"],
        s["pro_tier"],
        s["pro_factor"],
    )

    def check(name, passed):
        checks[name] = "matched" if passed else "mismatch"
        if not passed:
            errors.append(name + "_mismatch")

    check(
        names[0],
        v.get("trader") == request["account"].lower()
        and v.get("referralCode") == s["code"]
        and v.get("affiliate") == s["affiliate"],
    )
    # GMX omits conditional referral/pro event fields for zero code / zero tier.
    check(
        names[1],
        all(v.get(k, 0) == n for k, n in expected.items() if k.startswith("referral.")),
    )
    check(
        names[2],
        all(v.get(k, 0) == n for k, n in expected.items() if k.startswith("pro.")),
    )
    check(names[3], v.get("protocolFeeAmount") == expected["protocolFeeAmount"])
    return {"configuration": s, "position_fee_amount": fee, "expected": expected}
