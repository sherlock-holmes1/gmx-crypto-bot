"""Collect opening and closing funding and borrowing state."""

from __future__ import annotations

import json

from gmx_crypto_bot_v2.domain.accrual import decode, slots
from gmx_crypto_bot_v2.domain.swap_keys import call_data
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash


def collect(recording, rpc):
    metadata = json.loads((recording / "metadata.json").read_text())
    q = json.loads((recording / "completeness-report.json").read_text())
    if not q.get("complete") or q.get("gaps") or q.get("reorgs"):
        raise ValueError("incomplete recording")
    store = metadata["contracts"]["data_store"].lower()
    specs = slots(metadata)
    result = {
        "schema": "GmxAccrualConfiguration",
        "version": 1,
        "market": metadata["market"]["market_token_address"].lower(),
        "data_store": store,
    }
    for label, block in [
        ("opening", q["source_block_range"]["from"] - 1),
        ("closing", q["source_block_range"]["to"]),
    ]:
        expected = _recorded_block_hash(recording, block)
        if rpc.call("eth_getBlockByNumber", [hex(block), False])["hash"] != expected:
            raise ValueError("archive block hash mismatch")
        calls = {}
        jobs = list(specs.items())
        for i in range(0, len(jobs), 40):
            group = jobs[i : i + 40]
            values = rpc.call_many(
                "eth_call",
                [
                    [
                        {
                            "to": store,
                            "data": call_data("get" + v["kind"] + "(bytes32)", k),
                        },
                        hex(block),
                    ]
                    for k, v in group
                ],
            )
            if len(values) != len(group):
                raise ValueError("incomplete archive batch")
            for (k, v), raw in zip(group, values):
                decode(raw, v["kind"])
                calls[k] = raw
        if rpc.call("eth_getBlockByNumber", [hex(block), False])["hash"] != expected:
            raise ValueError("archive block changed")
        result[label] = {"block_number": block, "block_hash": expected, "calls": calls}
    return result
