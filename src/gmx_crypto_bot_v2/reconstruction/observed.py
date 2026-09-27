"""Observable-state projection used by deterministic recording replay."""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from gmx_crypto_bot_v2.domain.checkpoint import normalize_recorded_order_checkpoint
from gmx_crypto_bot_v2.domain.events import EventDecodeError, decode_event_log
from gmx_crypto_bot_v2.evidence.recording import RecordedEvent

TERMINAL_EVENTS = {"OrderExecuted", "OrderCancelled", "OrderFrozen"}


ORDER_UPDATE_EVENTS = {
    "OrderUpdated",
    "OrderSizeDeltaAutoUpdated",
    "OrderCollateralDeltaAmountAutoUpdated",
}


def _coordinate(event: RecordedEvent) -> tuple[int, int, int, int]:
    return (
        event.block_number if event.block_number is not None else -1,
        event.transaction_index if event.transaction_index is not None else -1,
        event.log_index if event.log_index is not None else -1,
        event.seq,
    )


def _side_key(values: dict[str, Any]) -> str:
    return f"{values.get('collateralToken', 'unknown')}:{'long' if values.get('isLong') else 'short'}"


def _jsonable(value: Any) -> Any:
    """Return a canonical JSON-compatible copy without mutating raw evidence."""
    return json.loads(json.dumps(value, sort_keys=True))


@dataclass
class ReplayState:
    """The state known from a bounded recording, never inferred beyond its evidence."""

    target_market: str
    index_token: str
    opening_checkpoint_present: bool
    complete: bool = True
    incomplete_reasons: list[str] = field(default_factory=list)
    events_applied: int = 0
    decode_errors: int = 0
    data_gaps: list[dict[str, Any]] = field(default_factory=list)
    reorgs: list[dict[str, Any]] = field(default_factory=list)
    oracle: dict[str, dict[str, Any]] = field(default_factory=dict)
    configuration: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_interest_usd: dict[str, int] = field(default_factory=dict)
    open_interest_tokens: dict[str, int] = field(default_factory=dict)
    borrowing: dict[str, dict[str, Any]] = field(default_factory=dict)
    funding: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    fees: dict[str, dict[str, Any]] = field(default_factory=dict)
    event_counts: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        if not self.opening_checkpoint_present:
            self.mark_incomplete("opening_checkpoint_missing")

    def mark_incomplete(self, reason: str) -> None:
        self.complete = False
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)

    def apply(self, event: RecordedEvent) -> None:
        self.events_applied += 1
        if event.kind == "data_gap":
            self.data_gaps.append(_jsonable(event.payload))
            self.mark_incomplete("data_gap")
            return
        if event.kind == "reorg_detected":
            self.reorgs.append(_jsonable(event.payload))
            self.mark_incomplete("reorg_detected")
            return
        if event.kind not in {"gmx_market_log", "gmx_order_lifecycle_log"}:
            return

        try:
            decoded = decode_event_log(event.payload["log"]["data"])
        except (EventDecodeError, KeyError):
            self.decode_errors += 1
            self.mark_incomplete("event_decode_error")
            return
        name, values = decoded.event_name, decoded.values
        if (
            name
            not in {
                "OraclePriceUpdate",
                "SetUint",
                *TERMINAL_EVENTS,
                *ORDER_UPDATE_EVENTS,
            }
            and values.get("market") != self.target_market
        ):
            return
        self.event_counts[name] += 1
        if name == "OraclePriceUpdate":
            self.oracle[str(values["token"])] = {
                **_jsonable(values),
                "coordinate": _coordinate(event),
            }
        elif name == "SetUint":
            key = f"{values.get('baseKey')}:{values.get('data')}"
            self.configuration[key] = {
                **_jsonable(values),
                "coordinate": _coordinate(event),
            }
        elif name == "OpenInterestUpdated":
            self.open_interest_usd[_side_key(values)] = int(values["nextValue"])
        elif name == "OpenInterestInTokensUpdated":
            self.open_interest_tokens[_side_key(values)] = int(values["nextValue"])
        elif name == "CumulativeBorrowingFactorUpdated":
            self.borrowing.setdefault(_side_key(values), {})["cumulative_factor"] = (
                _jsonable(values)
            )
        elif name == "Borrowing":
            self.borrowing.setdefault("market", {})["factor_per_second"] = _jsonable(
                values
            )
        elif name == "FundingFeeAmountPerSizeUpdated":
            self.funding.setdefault(_side_key(values), {})["fee_amount_per_size"] = (
                _jsonable(values)
            )
        elif name == "Funding":
            self.funding.setdefault("market", {})["factor_per_second"] = _jsonable(
                values
            )
        elif name == "OrderCreated":
            key = str(values["key"]).lower()
            self.orders[key] = {
                "created": _jsonable(values),
                "created_coordinate": _coordinate(event),
                "oracle_at_request": copy.deepcopy(self.oracle.get(self.index_token)),
                "updates": [],
                "terminal": None,
            }
        elif name in ORDER_UPDATE_EVENTS:
            key = str(values.get("key", "")).lower()
            if key in self.orders:
                self.orders[key]["updates"].append(_jsonable(values))
        elif name in TERMINAL_EVENTS:
            key = str(values.get("key", "")).lower()
            if key in self.orders:
                order = self.orders[key]
                order["terminal"] = {
                    "event_name": name,
                    "values": _jsonable(values),
                    "coordinate": _coordinate(event),
                    "oracle_at_terminal": copy.deepcopy(
                        self.oracle.get(self.index_token)
                    ),
                }
        elif name in {"PositionIncrease", "PositionDecrease"}:
            key = str(values["positionKey"]).lower()
            self.positions[key] = {
                **_jsonable(values),
                "event_name": name,
                "coordinate": _coordinate(event),
            }
        elif name == "PositionFeesCollected":
            key = str(values["positionKey"]).lower()
            self.fees[key] = {**_jsonable(values), "coordinate": _coordinate(event)}

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "GmxReplayState",
            "version": 1,
            "target_market": self.target_market,
            "index_token": self.index_token,
            "opening_checkpoint_present": self.opening_checkpoint_present,
            "complete": self.complete,
            "incomplete_reasons": sorted(self.incomplete_reasons),
            "events_applied": self.events_applied,
            "decode_errors": self.decode_errors,
            "data_gaps": self.data_gaps,
            "reorgs": self.reorgs,
            "oracle": self.oracle,
            "configuration": self.configuration,
            "open_interest_usd": self.open_interest_usd,
            "open_interest_tokens": self.open_interest_tokens,
            "borrowing": self.borrowing,
            "funding": self.funding,
            "orders": self.orders,
            "positions": self.positions,
            "fees": self.fees,
            "event_counts": dict(sorted(self.event_counts.items())),
        }


def build_state(events: list[RecordedEvent], metadata: dict[str, Any]) -> ReplayState:
    """Apply canonical events to an observable, bounded replay state."""
    market = metadata["market"]
    checkpoint_data = metadata.get("opening_state_checkpoint")
    checkpoint_events = [
        event for event in events if event.kind == "opening_state_checkpoint"
    ]
    if checkpoint_data is None and checkpoint_events:
        if len(checkpoint_events) != 1:
            raise ValueError(
                "recording must contain exactly one opening state checkpoint"
            )
        checkpoint_data = checkpoint_events[0].payload
    checkpoint = isinstance(checkpoint_data, dict)
    state = ReplayState(
        target_market=str(market["market_token_address"]).lower(),
        index_token=str(
            metadata.get("tokens", {}).get("index", {}).get("address", "")
        ).lower(),
        opening_checkpoint_present=checkpoint,
    )
    if not state.index_token:
        state.mark_incomplete("index_token_missing_from_recording_metadata")
    if checkpoint:
        _load_checkpoint(state, checkpoint_data)
        if checkpoint_data.get("complete") is False:
            state.mark_incomplete("opening_checkpoint_incomplete")
    for event in events:
        if event.kind == "opening_state_checkpoint":
            continue
        state.apply(event)
    return state


def _load_checkpoint(state: ReplayState, checkpoint: dict[str, Any]) -> None:
    """Seed only explicitly supplied block-pinned observable state."""
    if (
        str(checkpoint.get("market_token_address", state.target_market)).lower()
        != state.target_market
    ):
        raise ValueError("opening checkpoint market does not match recording market")
    if not isinstance(checkpoint.get("block_number"), int):
        raise ValueError("opening checkpoint requires an integer block_number")
    for name in (
        "oracle",
        "configuration",
        "open_interest_usd",
        "open_interest_tokens",
        "borrowing",
        "funding",
        "orders",
        "positions",
        "fees",
    ):
        value = checkpoint.get(name)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"opening checkpoint {name} must be an object")
            if name == "orders":
                value = {
                    key: (
                        {
                            **order,
                            "opening_checkpoint": normalize_recorded_order_checkpoint(
                                order["opening_checkpoint"]
                            ),
                        }
                        if isinstance(order, dict)
                        and isinstance(order.get("opening_checkpoint"), dict)
                        else order
                    )
                    for key, order in value.items()
                }
            setattr(state, name, _jsonable(value))


def report_from_state(state: ReplayState) -> dict[str, Any]:
    """Summarize state changes required for replay calibration reports."""
    terminal = Counter()
    delays: list[int] = []
    oracle_moves: list[int] = []
    unresolved = 0
    for order in state.orders.values():
        result = order.get("terminal")
        if result is None:
            unresolved += 1
            continue
        terminal[result["event_name"]] += 1
        if order.get("created") is None:
            continue
        delays.append(result["coordinate"][0] - order["created_coordinate"][0])
        request_oracle = order.get("oracle_at_request")
        terminal_oracle = result.get("oracle_at_terminal")
        if request_oracle and terminal_oracle:
            oracle_moves.append(
                int(terminal_oracle["minPrice"]) - int(request_oracle["minPrice"])
            )
    long_oi = sum(
        value for key, value in state.open_interest_usd.items() if key.endswith(":long")
    )
    short_oi = sum(
        value
        for key, value in state.open_interest_usd.items()
        if key.endswith(":short")
    )
    return {
        "schema": "GmxReplayReport",
        "version": 1,
        "complete": state.complete,
        "incomplete_reasons": sorted(state.incomplete_reasons),
        "events_applied": state.events_applied,
        "decode_errors": state.decode_errors,
        "orders": {
            "tracked": len(state.orders),
            "created": sum(
                order.get("created") is not None for order in state.orders.values()
            ),
            "opening": sum(
                order.get("opening_checkpoint") is not None
                for order in state.orders.values()
            ),
            "unresolved": unresolved,
            "terminal_outcomes": dict(sorted(terminal.items())),
            "request_to_terminal_blocks": _distribution(delays),
            "oracle_min_price_move": _distribution(oracle_moves),
        },
        "market": {
            "configuration_changes": len(state.configuration),
            "open_interest_usd": {
                "long": long_oi,
                "short": short_oi,
                "imbalance": long_oi - short_oi,
            },
            "open_interest_tokens": state.open_interest_tokens,
        },
        "funding": state.funding,
        "borrowing": state.borrowing,
        "data_quality": {"gaps": len(state.data_gaps), "reorgs": len(state.reorgs)},
        "state": state.as_dict(),
    }


def _distribution(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) // len(values),
    }
