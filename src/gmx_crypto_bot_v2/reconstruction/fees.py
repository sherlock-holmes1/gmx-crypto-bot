"""Build block-bounded fee-configuration histories."""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from gmx_crypto_bot_v2.domain.configuration import fee_keys
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.snapshots import snapshot_path


@dataclass(frozen=True)
class ConfigHistory:
    start_block: int
    end_block: int
    changes: tuple[tuple[tuple[int, int, int], int], ...]
    opening: int | None = None
    closing: int | None = None

    def __post_init__(self):
        if (
            self.start_block > self.end_block
            or tuple(sorted(self.changes)) != self.changes
        ):
            raise ValueError("invalid configuration bounds or event order")
        if any(
            not self.start_block <= c[0] <= self.end_block
            or type(v) is not int
            or v < 0
            for c, v in self.changes
        ):
            raise ValueError(
                "configuration write outside evidence interval or invalid value"
            )
        for value in (self.opening, self.closing):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid configuration anchor")
        final = self.changes[-1][1] if self.changes else self.opening
        if final is not None and self.closing is not None and final != self.closing:
            raise ValueError("configuration history disagrees with closing anchor")

    def at(self, coordinate: tuple[int, int, int]) -> int | None:
        if not self.start_block <= coordinate[0] <= self.end_block:
            return None
        index = bisect_right([c for c, _ in self.changes], coordinate) - 1
        if index >= 0:
            return self.changes[index][1]
        if self.opening is not None:
            return self.opening
        # A complete no-write interval permits a closing anchor to establish
        # its unchanged value. Never carry a later write back in time.
        return self.closing if not self.changes else None


def build_fee_histories(
    recording: Path, metadata: dict, config_events: list[dict], receivers: set[str]
) -> dict[str, ConfigHistory]:
    report_path = recording / "completeness-report.json"
    if not report_path.exists():
        return {}
    report = json.loads(report_path.read_text())
    if report.get("complete") is not True or report.get("gaps") or report.get("reorgs"):
        return {}
    bounds = report.get("source_block_range", {})
    start, end = bounds.get("from"), bounds.get("to")
    if type(start) is not int or type(end) is not int:
        return {}
    keys = fee_keys(metadata["market"]["market_token_address"].lower(), receivers)
    wanted = {(key.base_key, key.data): field for field, key in keys.items()}
    changes: dict[str, dict[tuple[int, int, int], int]] = {field: {} for field in keys}
    for entry in config_events:
        v = entry["values"]
        if entry["event_name"] == "UiFeeFactorUpdated":
            field = "ui_fee:" + v.get("account", "").lower()
            value = v.get("uiFeeFactor")
        else:
            field = wanted.get((v.get("baseKey"), v.get("data")))
            value = v.get("value")
        if field not in changes:
            continue
        coordinate = (
            entry["block_number"],
            entry["transaction_index"],
            entry["log_index"],
        )
        if not start <= coordinate[0] <= end:
            continue
        if coordinate in changes[field] and changes[field][coordinate] != value:
            raise ValueError("conflicting duplicate configuration writes")
        changes[field][coordinate] = value
    snapshots = {}
    sidecar = snapshot_path(recording, "fee-opening-configuration.json")
    if sidecar.exists():
        snapshots = json.loads(sidecar.read_text())
        if (
            snapshots.get("schema") != "GmxFeeOpeningConfiguration"
            or snapshots.get("version") != 1
            or snapshots.get("market")
            != metadata["market"]["market_token_address"].lower()
            or snapshots.get("data_store")
            != metadata["contracts"]["data_store"].lower()
        ):
            raise ValueError("fee configuration sidecar identity mismatch")
        for label, block in [("opening", start - 1)]:
            point = snapshots.get(label, {})
            expected_hash = _recorded_block_hash(recording, block)
            if (
                point.get("block_number") != block
                or point.get("block_hash") != expected_hash
            ):
                raise ValueError("fee configuration sidecar block mismatch")
            for field, item in point.get("values", {}).items():
                if (
                    field not in keys
                    or item.get("storage_key") != keys[field].storage_key
                ):
                    raise ValueError("fee configuration sidecar storage key mismatch")
                raw = item.get("result", "")
                if (
                    len(raw) != 66
                    or not raw.startswith("0x")
                    or int(raw, 16) != item.get("value")
                ):
                    raise ValueError("fee configuration sidecar value mismatch")
    histories = {}
    for field, key in keys.items():
        opening = (
            snapshots.get("opening", {}).get("values", {}).get(field, {}).get("value")
        )
        closing = None
        if (
            closing is None
            and metadata.get("pinned_configuration_anchor_block") == end
            and key.pinned_field
        ):
            pinned = metadata.get("pinned_configuration_raw", {}).get(key.pinned_field)
            closing = int(pinned) if pinned is not None else None
        histories[field] = ConfigHistory(
            start, end, tuple(sorted(changes[field].items())), opening, closing
        )
    return histories
