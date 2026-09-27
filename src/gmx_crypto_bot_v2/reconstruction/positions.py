"""Recover position, inventory and pool context immediately before execution."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.domain.entries import _coordinate
from gmx_crypto_bot_v2.models.arithmetic import _proportional_pending_impact


def _attach_pre_position_state(
    events: list[dict[str, Any]],
    opening_positions: dict[str, dict[str, Any]],
    borrowing_events: list[dict[str, Any]],
    opening_borrowing: dict[str, Any],
    oracle_events: list[dict[str, Any]] | None = None,
    index_token: str | None = None,
    open_interest_events: list[dict[str, Any]] | None = None,
    opening_open_interest_tokens: dict[str, int] | None = None,
) -> None:
    """Reconstruct the position immediately before each observed position event."""
    positions = {key.lower(): dict(value) for key, value in opening_positions.items()}
    borrowing = {
        side: opening_borrowing.get(side, {})
        .get("cumulative_factor", {})
        .get("nextCumulativeBorrowingFactor")
        for side in ("long", "short")
    }
    oracle_at_transaction: dict[str, dict[str, Any]] = {}
    oracle_transaction: str | None = None
    interest = dict(opening_open_interest_tokens or {})
    latest_interest_update: dict[str, dict[str, Any]] = {}
    for entry in sorted(
        events
        + borrowing_events
        + (oracle_events or [])
        + (open_interest_events or []),
        key=_coordinate,
    ):
        values = entry["values"]
        if entry["transaction_hash"] != oracle_transaction:
            oracle_transaction = entry["transaction_hash"]
            oracle_at_transaction = {}
        if entry["event_name"] == "OraclePriceUpdate":
            token = values.get("token")
            if isinstance(token, str):
                oracle_at_transaction[token] = values
            continue
        if entry["event_name"] == "CumulativeBorrowingFactorUpdated":
            borrowing["long" if values.get("isLong") else "short"] = values.get(
                "nextValue"
            )
            continue
        if entry["event_name"] == "OpenInterestInTokensUpdated":
            side = "long" if values.get("isLong") else "short"
            cell = f"{values.get('collateralToken')}:{side}"
            interest[cell] = values.get("nextValue")
            latest_interest_update[cell] = entry
            continue
        key = values.get("positionKey")
        if not isinstance(key, str):
            continue
        key = key.lower()
        previous = positions.get(key)
        entry["pre_position"] = dict(previous) if previous is not None else None
        oracle = (
            oracle_at_transaction.get(index_token)
            if index_token
            else next(reversed(oracle_at_transaction.values()), None)
        )
        entry["oracle_at_event"] = dict(oracle) if oracle is not None else None
        entry["oracle_prices_at_event"] = {
            token: dict(price) for token, price in oracle_at_transaction.items()
        }
        is_long = (
            previous.get("isLong") if previous is not None else values.get("isLong")
        )
        entry["cumulative_borrowing_factor"] = borrowing["long" if is_long else "short"]
        if entry["event_name"] == "PositionFeesCollected":
            continue
        side = "long" if values.get("isLong") else "short"
        cell = f"{values.get('collateralToken')}:{side}"
        size_delta_tokens = values.get("sizeDeltaInTokens")
        signed_delta = (
            size_delta_tokens
            if entry["event_name"] == "PositionIncrease"
            else -size_delta_tokens
            if isinstance(size_delta_tokens, int)
            else None
        )
        update = latest_interest_update.get(cell)
        update_matches = (
            isinstance(signed_delta, int)
            and isinstance(interest.get(cell), int)
            and (
                signed_delta == 0
                or (
                    update is not None
                    and update["transaction_hash"] == entry["transaction_hash"]
                    and update["values"].get("delta") == signed_delta
                )
            )
        )
        if update_matches:
            before = dict(interest)
            before[cell] -= signed_delta
            entry["pre_open_interest_tokens"] = {
                side_name: sum(
                    value
                    for name, value in before.items()
                    if name.endswith(f":{side_name}")
                )
                for side_name in ("long", "short")
            }
        entry["open_interest_update_matches"] = update_matches
        if not values.get("sizeInUsd"):
            positions.pop(key, None)
            continue
        next_position = dict(previous or {})
        for field in (
            "account",
            "market",
            "collateralToken",
            "isLong",
            "sizeInUsd",
            "sizeInTokens",
            "collateralAmount",
            "borrowingFactor",
            "fundingFeeAmountPerSize",
            "longTokenClaimableFundingAmountPerSize",
            "shortTokenClaimableFundingAmountPerSize",
            "pendingImpactAmount",
        ):
            if field in values:
                next_position[field] = values[field]
        old_pending = (previous or {}).get("pendingImpactAmount", 0)
        if entry["event_name"] == "PositionIncrease" and isinstance(
            values.get("pendingPriceImpactAmount"), int
        ):
            next_position["pendingImpactAmount"] = (
                old_pending + values["pendingPriceImpactAmount"]
            )
        elif entry["event_name"] == "PositionDecrease" and previous is not None:
            size = previous.get("sizeInUsd")
            delta = values.get("sizeDeltaUsd")
            if (
                isinstance(old_pending, int)
                and isinstance(size, int)
                and size > 0
                and isinstance(delta, int)
            ):
                next_position["pendingImpactAmount"] = (
                    old_pending - _proportional_pending_impact(old_pending, delta, size)
                )
        positions[key] = next_position


def _attach_pre_virtual_inventory(
    position_events: list[dict[str, Any]],
    virtual_events: list[dict[str, Any]],
) -> None:
    """Reconstruct pre-trade virtual inventory from all recorded markets sharing the token ID."""
    if not virtual_events:
        return
    first = min(virtual_events, key=_coordinate)["values"]
    inventory = first["nextValue"] - first["delta"]
    latest: dict[str, Any] | None = None
    continuous = True
    positions = [
        event
        for event in position_events
        if event["event_name"] in {"PositionIncrease", "PositionDecrease"}
    ]
    for entry in sorted(virtual_events + positions, key=_coordinate):
        values = entry["values"]
        if entry["event_name"] == "VirtualPositionInventoryUpdated":
            if values["nextValue"] - values["delta"] != inventory:
                continuous = False
            inventory = values["nextValue"]
            latest = entry
            continue
        size = values.get("sizeDeltaInTokens")
        if not isinstance(size, int):
            entry["virtual_inventory_update_matches"] = False
            continue
        signed_size = size if entry["event_name"] == "PositionIncrease" else -size
        expected_delta = -signed_size if values.get("isLong") else signed_size
        matches = expected_delta == 0 or (
            latest is not None
            and latest["transaction_hash"] == entry["transaction_hash"]
            and latest["values"].get("delta") == expected_delta
        )
        entry["virtual_inventory_update_matches"] = matches
        entry["virtual_inventory_continuous"] = continuous
        if matches and continuous:
            entry["pre_virtual_inventory_tokens"] = inventory - expected_delta


def _attach_pre_impact_pool(
    position_events: list[dict[str, Any]],
    pool_events: list[dict[str, Any]],
) -> None:
    """Recover position-impact pool amounts from event next values and deltas."""
    if not pool_events:
        return
    first = min(pool_events, key=_coordinate)["values"]
    amount = first["nextValue"] - first["delta"]
    latest: dict[str, Any] | None = None
    continuous = True
    positions = [
        event
        for event in position_events
        if event["event_name"] in {"PositionIncrease", "PositionDecrease"}
    ]
    for entry in sorted(pool_events + positions, key=_coordinate):
        if entry["event_name"] == "PositionImpactPoolAmountUpdated":
            values = entry["values"]
            if values["nextValue"] - values["delta"] != amount:
                continuous = False
            amount = values["nextValue"]
            latest = entry
            continue
        entry["impact_pool_continuous"] = continuous
        if continuous:
            if (
                latest is not None
                and latest["transaction_hash"] == entry["transaction_hash"]
            ):
                entry["pre_impact_pool_amount"] = amount - latest["values"]["delta"]
            else:
                entry["pre_impact_pool_amount"] = amount
        latest = None
