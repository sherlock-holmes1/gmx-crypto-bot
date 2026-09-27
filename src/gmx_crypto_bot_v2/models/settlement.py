"""Pure liquidation cash flow and independently reconstructed fee inputs."""

from __future__ import annotations

ZERO = "0x" + "0" * 40


def coordinate(event: dict) -> tuple[int, int, int]:
    return tuple(
        event[name] for name in ("block_number", "transaction_index", "log_index")
    )


from dataclasses import asdict, dataclass
from typing import Any

from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.models.accrual import P, ceil_div, liquidation_fee
from gmx_crypto_bot_v2.models.referral import discount_amounts


@dataclass
class Cash:
    collateral: int
    output: int
    secondary: int
    collateral_price: int
    secondary_price: int

    def pay(self, usd: int) -> dict[str, int]:
        if usd < 0 or min(self.collateral_price, self.secondary_price) <= 0:
            raise ValueError("invalid settlement cost or price")
        needed = ceil_div(usd, self.collateral_price)
        output_paid = min(self.output, needed)
        self.output -= output_paid
        needed -= output_paid
        collateral_paid = min(self.collateral, needed)
        self.collateral -= collateral_paid
        needed -= collateral_paid
        secondary_needed = needed * self.collateral_price // self.secondary_price
        secondary_paid = min(self.secondary, secondary_needed)
        self.secondary -= secondary_paid
        return {
            "cost_usd": usd,
            "paid_in_collateral": output_paid + collateral_paid,
            "paid_in_secondary": secondary_paid,
            "remaining_cost_usd": (secondary_needed - secondary_paid)
            * self.secondary_price,
        }


def settle(
    cash: Cash,
    base: int,
    impact: int,
    impact_diff: int,
    funding: int,
    fees_excluding_funding: int,
) -> dict[str, Any]:
    """Stop at the first unpaid cost; never charge later costs after insolvency."""
    payments = []
    insolvent_step = None
    fees_erased = False
    for step, cost in (
        ("funding", funding * cash.collateral_price),
        ("pnl", max(-base, 0)),
        ("fees", fees_excluding_funding * cash.collateral_price),
        ("impact", max(-impact, 0)),
        ("diff", impact_diff),
    ):
        payment = cash.pay(cost)
        payments.append(dict(step=step, **payment))
        if step == "fees" and payment["paid_in_collateral"] < fees_excluding_funding:
            fees_erased = True
        if payment["remaining_cost_usd"]:
            insolvent_step = step
            fees_erased = True
            break
    # DecreasePositionUtils releases all collateral on a full close.
    cash.output += cash.collateral
    cash.collateral = 0
    return dict(
        cash=asdict(cash),
        payments=payments,
        insolvent_step=insolvent_step,
        fees_erased=fees_erased,
    )


def reconstruct_fees(
    replay: Any,
    order_key: str,
    request: dict,
    position: dict,
    fees: dict,
    impact: dict,
    histories: dict,
    referral: Any,
) -> dict:
    state = replay.orders[order_key]
    previous = position["pre_position"]
    values = position["values"]
    c = coordinate(fees)
    token = values["collateralToken"]
    side = values["isLong"]
    price = position["oracle_prices_at_event"][token]["minPrice"]
    factors = {name: history.at(c) for name, history in histories.items()}
    improved = impact["balance_was_improved"]
    if type(improved) is not bool:
        raise ValueError("missing independently reconstructed impact direction")
    position_fee = (
        values["sizeDeltaUsd"]
        * factors["position_fee_positive" if improved else "position_fee_negative"]
        // P
        // price
    )
    config = referral.at(request["account"].lower(), c)
    discounts = discount_amounts(
        position_fee,
        config["code"],
        config["rebate_bps"],
        config["share_bps"],
        config["minimum"],
        config["pro_tier"],
        config["pro_factor"],
    )
    borrowing_usd = (
        previous["sizeInUsd"]
        * (state["borrowing"][side]["next_value"] - previous["borrowingFactor"])
        // P
    )
    latest_funding = state["funding_values"][
        key("FUNDING_FEE_AMOUNT_PER_SIZE", replay.market, token, side)
    ]
    funding_delta = latest_funding - previous["fundingFeeAmountPerSize"]
    funding = ceil_div(previous["sizeInUsd"] * funding_delta, 10**45)
    if min(borrowing_usd, funding_delta) < 0:
        raise ValueError("negative liquidation accrual")
    liquidation = liquidation_fee(
        values["sizeDeltaUsd"],
        price,
        state["liquidation_factor"],
        state["liquidation_receiver"],
    )
    receiver = request["uiFeeReceiver"]
    ui = (
        0
        if receiver == ZERO
        else values["sizeDeltaUsd"]
        * min(factors["ui_fee:" + receiver], factors["max_ui_fee"])
        // P
        // price
    )
    result = dict(
        positionFeeAmount=position_fee,
        borrowingFeeUsd=borrowing_usd,
        borrowingFeeAmount=borrowing_usd // price,
        fundingFeeAmount=funding,
        uiFeeAmount=ui,
        latestFundingFeeAmountPerSize=latest_funding,
        **liquidation,
    )
    result["totalCostAmount"] = (
        position_fee
        + borrowing_usd // price
        + funding
        + ui
        + liquidation["liquidationFeeAmount"]
        - discounts["totalDiscountAmount"]
    )
    for side_name, collateral in zip(("Long", "Short"), replay.tokens):
        latest = state["funding_values"][
            key("CLAIMABLE_FUNDING_AMOUNT_PER_SIZE", replay.market, collateral, side)
        ]
        result["latest" + side_name + "TokenClaimableFundingAmountPerSize"] = latest
        delta = (
            latest - previous[side_name.lower() + "TokenClaimableFundingAmountPerSize"]
        )
        if delta < 0:
            raise ValueError("negative claimable funding accrual")
        result["claimable" + side_name + "TokenAmount"] = (
            previous["sizeInUsd"] * delta // 10**45
        )
    return result
