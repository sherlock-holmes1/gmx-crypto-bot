"""Version position-impact factors from opening, closing and change evidence."""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from gmx_crypto_bot_v2.domain.events import decode_event_log, event_name_from_data
from gmx_crypto_bot_v2.domain.keys import (
    FACTOR_FIELDS,
    MARKET_FIELDS,
    config_base_key,
    config_market_data,
    config_market_side_data,
)
from gmx_crypto_bot_v2.evidence.repository import event_rows
from gmx_crypto_bot_v2.evidence.snapshots import snapshot_path


@dataclass(frozen=True)
class HistoricalFactor:
    """A terminal snapshot plus all recorded changes to one GMX uint key."""

    anchor_block: int
    anchor_value: int
    changes: tuple[tuple[tuple[int, int, int], int], ...]
    opening_value: int | None = None

    def __post_init__(self) -> None:
        if tuple(sorted(self.changes)) != self.changes:
            raise ValueError("configuration changes must be in canonical order")
        if self.changes and self.changes[-1][0][0] > self.anchor_block:
            raise ValueError("configuration change occurs after anchor")
        if self.changes and self.changes[-1][1] != self.anchor_value:
            raise ValueError("final recorded configuration disagrees with anchor")
        if (
            not self.changes
            and self.opening_value is not None
            and self.opening_value != self.anchor_value
        ):
            raise ValueError("unchanged configuration disagrees with opening snapshot")

    def at(self, coordinate: tuple[int, int, int]) -> int | None:
        """Return the effective value, or None before the first observed write."""
        if coordinate[0] > self.anchor_block:
            return None
        locations = [location for location, _ in self.changes]
        index = bisect_right(locations, coordinate) - 1
        if index >= 0:
            return self.changes[index][1]
        if self.opening_value is not None:
            return self.opening_value
        if not self.changes:
            return self.anchor_value
        return None


def load_factor_histories(
    recording: Path, metadata: dict
) -> dict[str, HistoricalFactor]:
    """Version impact factors from recorded writes and the pinned terminal anchor."""
    anchor = metadata.get("pinned_configuration_anchor_block")
    pinned = metadata.get("pinned_configuration_raw", {})
    market = metadata["market"]["market_token_address"].lower()
    if not isinstance(anchor, int):
        return {}
    wanted = {
        (config_base_key(name), config_market_side_data(market, positive)): field
        for (name, positive), field in FACTOR_FIELDS.items()
    }
    wanted.update(
        {
            (config_base_key(name), config_market_data(market)): field
            for name, field in MARKET_FIELDS.items()
        }
    )
    changes: dict[str, list[tuple[tuple[int, int, int], int]]] = {
        field: [] for field in wanted.values()
    }
    checkpoint: dict | None = None
    for item in event_rows(recording):
        if item.get("kind") == "opening_state_checkpoint":
            checkpoint = item["payload"]
        log = item.get("payload", {}).get("log")
        if not log or event_name_from_data(log.get("data", "")) != "SetUint":
            continue
        values = decode_event_log(log["data"]).values
        field = wanted.get((values.get("baseKey"), values.get("data")))
        if field is None:
            continue
        coordinate = (
            item["block_number"],
            item["transaction_index"],
            item["log_index"],
        )
        if coordinate[0] > anchor:
            raise ValueError(
                "position-impact configuration write occurs after pinned anchor"
            )
        changes[field].append((coordinate, values["value"]))
    opening_values: dict[str, int] = {}
    sidecar = snapshot_path(recording, "impact-opening-configuration.json")
    if sidecar.exists():
        snapshot = json.loads(sidecar.read_text(encoding="utf-8"))
        if (
            checkpoint is None
            or snapshot.get("schema") != "GmxImpactOpeningConfiguration"
            or snapshot.get("version") != 1
            or snapshot.get("block_number") != checkpoint.get("block_number")
            or snapshot.get("block_hash") != checkpoint.get("block_hash")
            or snapshot.get("market") != market
        ):
            raise ValueError(
                "impact opening configuration does not match recording checkpoint"
            )
        opening_values = {
            field: int(value) for field, value in snapshot.get("factors", {}).items()
        }
    return {
        field: HistoricalFactor(
            anchor, int(pinned[field]), tuple(sorted(events)), opening_values.get(field)
        )
        for field, events in changes.items()
        if field in pinned
    }
