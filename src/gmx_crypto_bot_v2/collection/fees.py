"""Collect block-pinned position and UI fee configuration."""

from __future__ import annotations

import json
from pathlib import Path

from gmx_crypto_bot_v2.domain.configuration import fee_keys
from gmx_crypto_bot_v2.domain.events import decode_event_log, event_name_from_data
from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.repository import event_rows


def fetch_opening_fee_configuration(recording: Path, rpc) -> dict:
    metadata = json.loads((recording / "metadata.json").read_text())
    report = json.loads((recording / "completeness-report.json").read_text())
    if report.get("complete") is not True or report.get("gaps") or report.get("reorgs"):
        raise ValueError("configuration backfill requires a complete recording")
    block = report["source_block_range"]["from"] - 1
    expected_hash = _recorded_block_hash(recording, block)
    header = rpc("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict) or header.get("hash") != expected_hash:
        raise ValueError("archive opening block hash differs from recording")
    receivers = set()
    for e in event_rows(recording):
        data = e.get("payload", {}).get("log", {}).get("data", "")
        if event_name_from_data(data) == "PositionFeesCollected":
            receiver = decode_event_log(data).values.get("uiFeeReceiver")
            if isinstance(receiver, str):
                receivers.add(receiver.lower())
    market = metadata["market"]["market_token_address"].lower()
    store = metadata["contracts"]["data_store"].lower()
    selector = keccak256(b"getUint(bytes32)")[:4].hex()
    values = {}
    for field, key in fee_keys(market, receivers).items():
        data = "0x" + selector + key.storage_key[2:]
        result = rpc("eth_call", [{"to": store, "data": data}, hex(block)])
        if (
            not isinstance(result, str)
            or len(result) != 66
            or not result.startswith("0x")
        ):
            raise ValueError("invalid historical uint result for " + field)
        values[field] = {
            "storage_key": key.storage_key,
            "result": result,
            "value": int(result, 16),
        }
    # Recheck after all number-pinned calls to detect an intervening reorg.
    header = rpc("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict) or header.get("hash") != expected_hash:
        raise ValueError("archive opening block changed during backfill")
    return {
        "schema": "GmxFeeOpeningConfiguration",
        "version": 1,
        "market": market,
        "data_store": store,
        "opening": {
            "block_number": block,
            "block_hash": expected_hash,
            "values": values,
        },
    }
