"""Compare recorded position fees with historical configuration."""

from __future__ import annotations

from gmx_crypto_bot_v2.domain.configuration import PRECISION, ZERO_ADDRESS
from gmx_crypto_bot_v2.reconstruction.fees import ConfigHistory


def compare_historical_fees(
    request: dict,
    position: dict,
    fees: dict,
    model: dict | None,
    histories: dict[str, ConfigHistory],
    checks: dict,
    mismatches: list,
) -> dict:
    coordinate = (fees["block_number"], fees["transaction_index"], fees["log_index"])
    factors = {field: history.at(coordinate) for field, history in histories.items()}
    values = fees["values"]
    if request.get("orderType") == 7:
        # Insolvent liquidations may erase the fee struct. Do not interpret its
        # zero factors as a configuration change or declare a passing check.
        checks["historical_fee_factors_liquidation"] = "unavailable"
        return factors

    def compare(name, actual, expected):
        if expected is None or actual is None:
            checks[name] = "unavailable"
        elif actual == expected:
            checks[name] = "matched"
        else:
            checks[name] = "mismatch"
            mismatches.append(name + "_mismatch")

    improved = model.get("balance_was_improved") if model else None
    expected = (
        factors.get("position_fee_positive" if improved else "position_fee_negative")
        if type(improved) is bool
        else None
    )
    compare("historical_position_fee_factor", values.get("positionFeeFactor"), expected)
    price, size = (
        values.get("collateralTokenPrice.min"),
        position["values"].get("sizeDeltaUsd"),
    )
    amount = (
        size * expected // PRECISION // price
        if expected is not None
        and type(price) is int
        and price > 0
        and type(size) is int
        else None
    )
    compare("historical_position_fee_amount", values.get("positionFeeAmount"), amount)
    for label, observed in [
        ("position_fee_receiver", "positionFeeReceiverFactor"),
        ("borrowing_fee_receiver", "borrowingFeeReceiverFactor"),
    ]:
        compare(
            "historical_" + label + "_factor", values.get(observed), factors.get(label)
        )
    receiver = request.get("uiFeeReceiver")
    ui = 0 if receiver == ZERO_ADDRESS else None
    if receiver and receiver != ZERO_ADDRESS:
        raw, cap = factors.get("ui_fee:" + receiver.lower()), factors.get("max_ui_fee")
        if raw is not None and cap is not None:
            ui = min(raw, cap)
    compare("historical_ui_fee_factor", values.get("uiFeeReceiverFactor"), ui)
    ui_amount = (
        size * ui // PRECISION // price
        if ui is not None and type(price) is int and price > 0 and type(size) is int
        else None
    )
    compare("historical_ui_fee_amount", values.get("uiFeeAmount"), ui_amount)
    return factors
