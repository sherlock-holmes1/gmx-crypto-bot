"""Collect and verify opening position-impact configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from gmx_crypto_bot_v2.domain.keys import FACTOR_FIELDS, config_base_key, keccak256
from gmx_crypto_bot_v2.evidence.repository import event_rows


def fetch_opening_impact_factors(
    recording: Path,
    rpc: Callable[[str, list[Any]], Any],
) -> dict[str, Any]:
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    market = metadata["market"]["market_token_address"].lower()
    data_store = metadata["contracts"]["data_store"]
    checkpoint = None
    for event in event_rows(recording):
        if event.get("kind") == "opening_state_checkpoint":
            checkpoint = event["payload"]
            break
    if checkpoint is None:
        raise ValueError("recording has no opening state checkpoint")
    block = checkpoint["block_number"]
    block_tag = hex(block)
    header = rpc("eth_getBlockByNumber", [block_tag, False])
    if (
        not isinstance(header, dict)
        or header.get("hash", "").lower() != checkpoint["block_hash"].lower()
    ):
        raise ValueError("archive RPC opening block hash differs from the recording")
    selector = keccak256(b"getUint(bytes32)")[:4]
    factors: dict[str, int] = {}
    raw_calls: dict[str, dict[str, str]] = {}
    for (name, positive), field in FACTOR_FIELDS.items():
        if name not in {"POSITION_IMPACT_FACTOR", "POSITION_IMPACT_EXPONENT_FACTOR"}:
            continue
        base = bytes.fromhex(config_base_key(name)[2:])
        key = keccak256(
            base
            + int(market, 16).to_bytes(32, "big")
            + int(positive).to_bytes(32, "big")
        )
        call_data = "0x" + (selector + key).hex()
        result = rpc("eth_call", [{"to": data_store, "data": call_data}, block_tag])
        if (
            not isinstance(result, str)
            or len(result) != 66
            or not result.startswith("0x")
        ):
            raise ValueError(f"archive RPC returned an invalid {field} value")
        factors[field] = int(result, 16)
        raw_calls[field] = {"storage_key": "0x" + key.hex(), "result": result}
    if (
        rpc("eth_getBlockByNumber", [block_tag, False]).get("hash", "").lower()
        != checkpoint["block_hash"].lower()
    ):
        raise ValueError("opening block changed during impact capture")
    return {
        "schema": "GmxImpactOpeningConfiguration",
        "version": 1,
        "block_number": block,
        "block_hash": checkpoint["block_hash"],
        "market": market,
        "data_store": data_store.lower(),
        "factors": factors,
        "raw_calls": raw_calls,
    }
