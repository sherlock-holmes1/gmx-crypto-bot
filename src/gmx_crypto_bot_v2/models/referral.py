"""Pure referral and pro discount arithmetic with protocol rounding."""

from __future__ import annotations

from gmx_crypto_bot_v2.domain.referral import ZERO_CODE, P


def discount_amounts(
    fee: int,
    code: str,
    rebate_bps: int,
    share_bps: int,
    minimum: int,
    pro_tier: int,
    pro_factor: int,
) -> dict[str, int]:
    if not 0 <= rebate_bps <= 10000 or not 0 <= share_bps <= 10000:
        raise ValueError("referral tier outside basis-point bounds")
    if not 0 <= pro_factor <= P or not 0 <= minimum <= P or fee < 0:
        raise ValueError("invalid discount factor")
    total = rebate_bps * P // 10000 if code != ZERO_CODE else 0
    # Solidity rounds the basis-point product before converting to 30 decimals.
    discount = (
        (rebate_bps * share_bps // 10000) * P // 10000 if code != ZERO_CODE else 0
    )
    affiliate = total - discount
    effective_pro = pro_factor if pro_tier else 0
    adjusted = affiliate
    if code != ZERO_CODE and effective_pro > discount:
        adjusted = (
            max(minimum, total - effective_pro) if effective_pro <= total else minimum
        )
    referral_amount = fee * discount // P
    affiliate_amount = fee * adjusted // P if code != ZERO_CODE else 0
    pro_amount = fee * effective_pro // P
    maximum = max(referral_amount, pro_amount)
    protocol = fee - affiliate_amount - maximum
    if protocol < 0:
        raise ValueError("discounts exceed position fee")
    return {
        "referral.totalRebateFactor": total,
        "referral.traderDiscountFactor": discount,
        "referral.adjustedAffiliateRewardFactor": adjusted,
        "referral.affiliateRewardAmount": affiliate_amount,
        "referral.traderDiscountAmount": referral_amount,
        "referral.totalRebateAmount": affiliate_amount + referral_amount,
        "pro.traderTier": pro_tier,
        "pro.traderDiscountFactor": effective_pro,
        "pro.traderDiscountAmount": pro_amount,
        "protocolFeeAmount": protocol,
        "totalDiscountAmount": maximum,
    }
