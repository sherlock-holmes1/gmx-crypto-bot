"""Compare independent liquidation settlement with recorded outcomes."""

from __future__ import annotations

from collections import Counter
from typing import Any

from gmx_crypto_bot_v2.checks.payouts import verify_payouts
from gmx_crypto_bot_v2.evidence.traces import TraceEvidenceSource
from gmx_crypto_bot_v2.models.settlement import Cash, reconstruct_fees, settle

MULTICHAIN_VAULT = "0xceaadfaf6a8c489b250e407987877c5fdfcdbe6e"


def compare_liquidation_settlement(
    order_key: str,
    request: dict,
    position: dict,
    fees: dict,
    swaps: list,
    payouts: list,
    metadata: dict,
    replay: Any,
    impact: dict,
    histories: dict,
    referral: Any,
    checks: dict,
    errors: list,
    recording: TraceEvidenceSource | None,
    receipt: dict | None,
    payout_receipt_available: bool,
) -> dict:
    result: dict[str, Any] = {"status": "unavailable"}
    checks["liquidation_settlement"] = "unavailable"
    required = (
        "historical_fee_factors_liquidation",
        "independent_funding_accumulators",
        "independent_borrowing_accumulators",
        "independent_price_impact",
        "uncapped_pnl",
        "proportional_pending_impact",
        "position_size_usd",
        "position_size_tokens",
        "decrease_tokens",
    )
    if any(checks.get(name) != "matched" for name in required):
        result["reason"] = "unverified liquidation economics or pre-position"
        return result
    if (
        not payout_receipt_available
        or receipt is None
        or receipt["payload"].get("status") != "0x1"
    ):
        result["reason"] = "missing successful payout receipt"
        return result
    try:
        values, previous = position["values"], position["pre_position"]
        if values["basePnlUsd"] != values["uncappedBasePnlUsd"]:
            raise ValueError("capped liquidation PnL is not independently modeled")
        if (
            values["sizeDeltaUsd"] != previous["sizeInUsd"]
            or request["initialCollateralDeltaAmount"] != 0
        ):
            raise ValueError("unsupported partial liquidation or withdrawal")
        token = values["collateralToken"]
        pnl_token = metadata["tokens"]["long" if values["isLong"] else "short"][
            "address"
        ].lower()
        prices = position["oracle_prices_at_event"]
        price, pnl_price = prices[token]["minPrice"], prices[pnl_token]
        reconstructed = reconstruct_fees(
            replay, order_key, request, position, fees, impact, histories, referral
        )
        profit = (
            max(values["basePnlUsd"], 0) // pnl_price["maxPrice"]
            + max(values["totalImpactUsd"], 0) // pnl_price["maxPrice"]
        )
        cash = Cash(
            previous["collateralAmount"],
            profit if pnl_token == token else 0,
            profit if pnl_token != token else 0,
            price,
            pnl_price["minPrice"],
        )
        hops = [e for e in swaps if e["event_name"] == "SwapInfo"]
        if request.get("decreasePositionSwapType") not in (0, 1):
            raise ValueError("unsupported liquidation output swap mode")
        if hops:
            if any(
                checks.get(name) != "matched"
                for name in (
                    "historical_swap_fees",
                    "independent_swap_price_impact",
                    "independent_swap_state",
                    "independent_swap_output",
                )
            ):
                raise ValueError("unverified liquidation swap")
            hop = hops[0]["values"]
            if (
                len(hops) != 1
                or coordinate(hops[0]) >= coordinate(position)
                or request["decreasePositionSwapType"] != 1
                or hop["tokenIn"] != pnl_token
                or hop["tokenOut"] != token
                or hop["amountIn"] != cash.secondary
                or hop["receiver"] != request["market"]
            ):
                raise ValueError("unsupported liquidation swap path")
            cash.output += hop["amountOut"]
            cash.secondary = 0
        result.update(
            settle(
                cash,
                values["basePnlUsd"],
                values["totalImpactUsd"],
                values.get("values.priceImpactDiffUsd", 0),
                reconstructed["fundingFeeAmount"],
                reconstructed["totalCostAmount"] - reconstructed["fundingFeeAmount"],
            )
        )
        result["reconstructed_fees"] = reconstructed
        expected = Counter()
        for output_token, amount in ((token, cash.output), (pnl_token, cash.secondary)):
            if amount:
                expected[(output_token, amount)] += 1
        payout_status = verify_payouts(
            expected, request, position, payouts, metadata, recording, receipt
        )
        observed_insolvency = [
            e
            for e in replay.insolvent.get(order_key, [])
            if e["transaction_hash"] == position["transaction_hash"]
        ]
        if result["insolvent_step"] is None:
            insolvency_matches = not observed_insolvency
        else:
            insolvency_matches = (
                len(observed_insolvency) == 1
                and all(
                    observed_insolvency[0]["values"].get(name) == value
                    for name, value in {
                        "step": result["insolvent_step"],
                        "remainingCostUsd": result["payments"][-1][
                            "remaining_cost_usd"
                        ],
                        "basePnlUsd": values["basePnlUsd"],
                        "positionCollateralAmount": previous["collateralAmount"],
                    }.items()
                )
                and coordinate(observed_insolvency[0]) < coordinate(fees)
            )
        expected_fees = dict(reconstructed)
        if result["fees_erased"]:
            expected_fees = {
                name: value if name.startswith(("claimable", "latest")) else 0
                for name, value in expected_fees.items()
            }
        fees_match = all(
            fees["values"].get(name, 0) == value
            for name, value in expected_fees.items()
        )
        if result["fees_erased"]:
            erased_fields = (
                "positionFeeFactor",
                "positionFeeReceiverFactor",
                "borrowingFeeReceiverFactor",
                "borrowingFeeAmountForFeeReceiver",
                "uiFeeReceiverFactor",
                "protocolFeeAmount",
                "feeReceiverAmount",
                "feeAmountForPool",
                "positionFeeAmountForPool",
                "totalCostAmountExcludingFunding",
                "totalDiscountAmount",
                "referral.totalRebateAmount",
                "referral.traderDiscountAmount",
                "referral.affiliateRewardAmount",
                "pro.traderDiscountAmount",
            )
            fees_match &= all(
                fees["values"].get(name, 0) == 0 for name in erased_fields
            )
        collateral_matches = (
            values["collateralAmount"] == 0
            and values["collateralDeltaAmount"] == previous["collateralAmount"]
            and values["sizeInUsd"] == values["sizeInTokens"] == 0
        )
        result.update(
            collateral_matches=collateral_matches,
            fees_match=fees_match,
            insolvency_matches=insolvency_matches,
            payout_status=payout_status,
            expected_outputs=[
                dict(token=t, amount=a, count=n) for (t, a), n in expected.items()
            ],
        )
        exact = collateral_matches and fees_match and insolvency_matches
        result["status"] = (
            "mismatch" if not exact or payout_status == "mismatch" else payout_status
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        result["reason"] = str(error)
    checks["liquidation_settlement"] = result["status"]
    if result["status"] == "mismatch":
        errors.append("liquidation_settlement_mismatch")
    return result


from gmx_crypto_bot_v2.models.settlement import coordinate
