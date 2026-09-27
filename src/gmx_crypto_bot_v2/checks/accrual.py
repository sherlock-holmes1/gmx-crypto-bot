"""Funding, borrowing and liquidation fee-configuration comparisons."""

from __future__ import annotations

from gmx_crypto_bot_v2.models.accrual import liquidation_fee
from gmx_crypto_bot_v2.reconstruction.accrual import coordinate, key


def compare_accrual(replay, order_key, checks, errors):
    state = replay.orders.get(order_key) if replay else None
    for label, name in [
        ("funding", "independent_funding_accumulators"),
        ("borrowing", "independent_borrowing_accumulators"),
    ]:
        values = (
            (
                [state.get("funding")]
                if label == "funding"
                else list(state.get("borrowing", {}).values())
            )
            if state
            else []
        )
        statuses = [v["status"] if v else "unavailable" for v in values]
        expected = 1 if label == "funding" else 2
        status = (
            "mismatch"
            if "mismatch" in statuses
            else (
                "matched"
                if len(statuses) == expected and set(statuses) == {"matched"}
                else "unavailable"
            )
        )
        checks[name] = status
        if status == "mismatch":
            errors.append(name + "_mismatch")


def compare_liquidation_configuration(
    replay,
    order_key,
    request,
    position,
    fees,
    impact,
    histories,
    checks,
    errors,
    referral=None,
    swaps=None,
):
    """Validate pre-settlement fees; never equate erased fees with zero settings."""
    from gmx_crypto_bot_v2.checks.fees import compare_historical_fees

    if request.get("orderType") != 7:
        return None
    state = replay.orders.get(order_key) if replay else None
    checks["historical_fee_factors_liquidation"] = "unavailable"
    if not state:
        return None
    candidate = fees
    stage = "collected"
    if (
        fees["values"].get("positionFeeFactor") == 0
        and fees["values"].get("totalCostAmount") == 0
    ):
        candidates = [
            e
            for e in replay.fee_info.get(order_key, [])
            if e["transaction_hash"] == fees["transaction_hash"]
            and coordinate(e) < coordinate(fees)
            and e["values"].get("positionKey") == fees["values"].get("positionKey")
        ]
        if not candidates or candidates[-1]["values"].get("totalCostAmount", 0) == 0:
            return compare_erased_liquidation(
                replay,
                state,
                order_key,
                request,
                position,
                fees,
                impact,
                histories,
                checks,
                errors,
                referral,
                swaps or [],
            )
        candidate = candidates[-1]
        stage = "pre_settlement_fee_info"
    values = candidate["values"]
    oracle = position.get("oracle_prices_at_event", {}).get(
        position["values"].get("collateralToken")
    )
    if not oracle:
        return {"stage": stage, "reason": "missing collateral oracle"}
    price = oracle["minPrice"]
    size = position["values"]["sizeDeltaUsd"]
    expected = liquidation_fee(
        size, price, state["liquidation_factor"], state["liquidation_receiver"]
    )
    local = {}
    local_errors = []
    compare_historical_fees(
        dict(request, orderType=4),
        position,
        candidate,
        impact,
        histories,
        local,
        local_errors,
    )
    exact = values.get("collateralTokenPrice.min") == price and all(
        values.get(k, 0) == v for k, v in expected.items()
    )
    status = (
        "mismatch"
        if not exact or "mismatch" in local.values()
        else (
            "matched" if local and set(local.values()) == {"matched"} else "unavailable"
        )
    )
    checks["historical_fee_factors_liquidation"] = status
    if status == "mismatch":
        errors.append("historical_fee_factors_liquidation_mismatch")
    return {
        "stage": stage,
        "coordinate": list(coordinate(candidate)),
        "factor": state["liquidation_factor"],
        "receiver_factor": state["liquidation_receiver"],
        "modeled": expected,
        "ordinary_fee_checks": local,
        "status": status,
        "settlement_verified": False,
    }


def compare_erased_liquidation(
    replay,
    state,
    order_key,
    request,
    position,
    fees,
    impact,
    histories,
    checks,
    errors,
    referral,
    swaps,
):
    """Corroborate erased settings via the independently reconstructed unpaid cost.

    Only full, loss-making liquidation closes at the fee-payment step are covered.
    Broader liquidation collateral settlement retains its separate gate.
    """
    from gmx_crypto_bot_v2.models.accrual import P, ceil_div
    from gmx_crypto_bot_v2.models.referral import discount_amounts

    result = {
        "stage": "erased_fee_insolvency",
        "settlement_verified": False,
        "status": "unavailable",
    }
    try:
        v = position["values"]
        prev = position["pre_position"]
        c = coordinate(fees)
        if prev["sizeInUsd"] != v["sizeDeltaUsd"] or v["basePnlUsd"] >= 0:
            raise ValueError("unsupported erased liquidation shape")
        side = v["isLong"]
        token = v["collateralToken"]
        prices = position["oracle_prices_at_event"]
        price = prices[token]["minPrice"]
        insolvent = [
            e
            for e in replay.insolvent[order_key]
            if e["transaction_hash"] == fees["transaction_hash"] and coordinate(e) < c
        ]
        if len(insolvent) != 1 or insolvent[0]["values"].get("step") != "fees":
            raise ValueError("missing fee-step insolvency evidence")
        if (
            not state["funding"]
            or state["funding"]["status"] != "matched"
            or len(state["borrowing"]) != 2
            or any(r["status"] != "matched" for r in state["borrowing"].values())
        ):
            raise ValueError("accrual derivation not verified")
        factors = {name: h.at(c) for name, h in histories.items()}
        improved = impact.get("balance_was_improved") if impact else None
        if type(improved) is not bool:
            raise ValueError("missing independent impact direction")
        factor = factors[
            "position_fee_positive" if improved else "position_fee_negative"
        ]
        position_fee = v["sizeDeltaUsd"] * factor // P // price
        config = referral.at(request["account"].lower(), c)
        discount = discount_amounts(
            position_fee,
            config["code"],
            config["rebate_bps"],
            config["share_bps"],
            config["minimum"],
            config["pro_tier"],
            config["pro_factor"],
        )["totalDiscountAmount"]
        next_borrow = state["borrowing"][side]["next_value"]
        borrowing = (
            prev["sizeInUsd"] * (next_borrow - prev["borrowingFactor"]) // P // price
        )
        next_funding = state["funding_values"][
            key("FUNDING_FEE_AMOUNT_PER_SIZE", replay.market, token, side)
        ]
        funding = ceil_div(
            prev["sizeInUsd"] * (next_funding - prev["fundingFeeAmountPerSize"]), 10**45
        )
        if min(borrowing, funding) < 0:
            raise ValueError("negative liquidation accrual")
        liquidation = liquidation_fee(
            v["sizeDeltaUsd"],
            price,
            state["liquidation_factor"],
            state["liquidation_receiver"],
        )
        receiver = request["uiFeeReceiver"]
        ui = 0
        if receiver != "0x" + "0" * 40:
            ui = (
                v["sizeDeltaUsd"]
                * min(factors["ui_fee:" + receiver], factors["max_ui_fee"])
                // P
                // price
            )
        cost = (
            position_fee
            + borrowing
            + liquidation["liquidationFeeAmount"]
            + ui
            - discount
        )
        # Base loss must itself agree with pre-position size/tokens and adverse oracle.
        index_price = prices[replay.index]["minPrice" if side else "maxPrice"]
        base = prev["sizeInTokens"] * index_price - prev["sizeInUsd"]
        if not side:
            base = -base
        if base != v["basePnlUsd"]:
            raise ValueError("unreconciled liquidation base loss")
        pnl_token = replay.tokens[0 if side else 1]
        pnl_price = prices[pnl_token]
        output = 0
        secondary = 0
        if v["totalImpactUsd"] > 0:
            amount = v["totalImpactUsd"] // pnl_price["maxPrice"]
            if pnl_token == token:
                output = amount
            else:
                secondary = amount
        if secondary and request["decreasePositionSwapType"] == 1:
            hops = [
                e
                for e in swaps
                if e["event_name"] == "SwapInfo"
                and coordinate(e) < coordinate(position)
            ]
            if (
                len(hops) != 1
                or hops[0]["values"]["amountIn"] != secondary
                or hops[0]["values"]["tokenIn"] != pnl_token
                or hops[0]["values"]["tokenOut"] != token
            ):
                raise ValueError("unreconciled liquidation impact conversion")
            output = hops[0]["values"]["amountOut"]
            secondary = 0
        collateral = prev["collateralAmount"]

        def pay(usd):
            nonlocal output, secondary, collateral
            needed = ceil_div(usd, price)
            taken = min(output, needed)
            output -= taken
            needed -= taken
            taken = min(collateral, needed)
            collateral -= taken
            needed -= taken
            if needed == 0:
                return 0
            secondary_needed = needed * price // pnl_price["minPrice"]
            taken = min(secondary, secondary_needed)
            secondary -= taken
            return (secondary_needed - taken) * pnl_price["minPrice"]

        if pay(funding * price) or pay(-base):
            raise ValueError("insolvency before fee step")
        remaining = pay(cost * price)
        observed = insolvent[0]["values"]
        erased_fields = (
            "positionFeeFactor",
            "positionFeeAmount",
            "borrowingFeeAmount",
            "borrowingFeeReceiverFactor",
            "positionFeeReceiverFactor",
            "fundingFeeAmount",
            "totalCostAmount",
            "liquidationFeeAmount",
            "liquidationFeeReceiverFactor",
            "liquidationFeeAmountForFeeReceiver",
        )
        exact = (
            remaining > 0
            and remaining == observed["remainingCostUsd"]
            and observed["basePnlUsd"] == base
            and observed["positionCollateralAmount"] == prev["collateralAmount"]
            and all(fees["values"].get(n, 0) == 0 for n in erased_fields)
        )
        result.update(
            status="matched" if exact else "mismatch",
            factor=state["liquidation_factor"],
            receiver_factor=state["liquidation_receiver"],
            modeled=liquidation,
            position_fee=position_fee,
            borrowing_fee=borrowing,
            funding_fee=funding,
            discount=discount,
            ui_fee=ui,
            remaining_cost_usd=remaining,
            observed_remaining_cost_usd=observed["remainingCostUsd"],
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        result["reason"] = str(error)
    checks["historical_fee_factors_liquidation"] = result["status"]
    if result["status"] == "mismatch":
        errors.append("historical_fee_factors_liquidation_mismatch")
    return result
