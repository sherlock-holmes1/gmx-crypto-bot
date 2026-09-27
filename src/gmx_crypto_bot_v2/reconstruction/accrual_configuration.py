"""Verify and decode historical accrual snapshot coverage."""

from __future__ import annotations

import json

from gmx_crypto_bot_v2.domain.accrual import (
    decode,
    slots,
)
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.snapshots import snapshot_path


def load_snapshot(recording, metadata):
    path = snapshot_path(recording, "accrual-configuration.json")
    if not path.exists():
        return None
    s = json.loads(path.read_text())
    q = json.loads((recording / "completeness-report.json").read_text())
    if not q.get("complete") or q.get("gaps") or q.get("reorgs"):
        raise ValueError("incomplete accrual recording")
    if (
        s.get("schema") != "GmxAccrualConfiguration"
        or s.get("version") != 1
        or s.get("market") != metadata["market"]["market_token_address"].lower()
        or s.get("data_store") != metadata["contracts"]["data_store"].lower()
    ):
        raise ValueError("accrual snapshot identity mismatch")
    specs = slots(metadata)
    points = {}
    for label, block in [
        ("opening", q["source_block_range"]["from"] - 1),
        ("closing", q["source_block_range"]["to"]),
    ]:
        point = s[label]
        if point["block_number"] != block or point[
            "block_hash"
        ] != _recorded_block_hash(recording, block):
            raise ValueError("accrual snapshot block mismatch")
        if set(point["calls"]) != set(specs):
            raise ValueError("accrual snapshot storage coverage mismatch")
        points[label] = {
            k: decode(point["calls"][k], v["kind"]) for k, v in specs.items()
        }
    return points
