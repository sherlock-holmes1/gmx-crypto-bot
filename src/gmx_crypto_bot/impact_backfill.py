"""Fetch the four impact factors missing before a recording's first SetUint.

Only read-only historical eth_call requests are made. The output is a small
block-hash-pinned sidecar consumed by the offline validator.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Callable

from gmx_crypto_bot.price_impact import FACTOR_FIELDS, config_base_key, keccak256


def _rpc_call(endpoint: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict) or "error" in result or "result" not in result:
        raise ValueError(f"historical RPC {method} failed: {result.get('error') if isinstance(result, dict) else result}")
    return result["result"]


def fetch_opening_impact_factors(
    recording: Path, rpc: Callable[[str, list[Any]], Any],
) -> dict[str, Any]:
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    market = metadata["market"]["market_token_address"].lower()
    data_store = metadata["contracts"]["data_store"]
    checkpoint = None
    with (recording / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("kind") == "opening_state_checkpoint":
                checkpoint = event["payload"]
                break
    if checkpoint is None:
        raise ValueError("recording has no opening state checkpoint")
    block = checkpoint["block_number"]
    block_tag = hex(block)
    header = rpc("eth_getBlockByNumber", [block_tag, False])
    if not isinstance(header, dict) or header.get("hash", "").lower() != checkpoint["block_hash"].lower():
        raise ValueError("archive RPC opening block hash differs from the recording")
    selector = keccak256(b"getUint(bytes32)")[:4]
    factors: dict[str, int] = {}
    raw_calls: dict[str, dict[str, str]] = {}
    for (name, positive), field in FACTOR_FIELDS.items():
        if name not in {"POSITION_IMPACT_FACTOR", "POSITION_IMPACT_EXPONENT_FACTOR"}:
            continue
        base = bytes.fromhex(config_base_key(name)[2:])
        key = keccak256(base + int(market, 16).to_bytes(32, "big") + int(positive).to_bytes(32, "big"))
        call_data = "0x" + (selector + key).hex()
        result = rpc("eth_call", [{"to": data_store, "data": call_data}, block_tag])
        if not isinstance(result, str) or len(result) != 66 or not result.startswith("0x"):
            raise ValueError(f"archive RPC returned an invalid {field} value")
        factors[field] = int(result, 16)
        raw_calls[field] = {"storage_key": "0x" + key.hex(), "result": result}
    return {
        "schema": "GmxImpactOpeningConfiguration", "version": 1,
        "block_number": block, "block_hash": checkpoint["block_hash"],
        "market": market, "data_store": data_store.lower(),
        "factors": factors, "raw_calls": raw_calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill opening GMX position-impact factors")
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    endpoint = os.environ.get("GMX_ARCHIVE_RPC_URL")
    if not endpoint:
        parser.error("set GMX_ARCHIVE_RPC_URL to an archive-capable Arbitrum RPC URL")
    payload = fetch_opening_impact_factors(args.recording, lambda method, params: _rpc_call(endpoint, method, params))
    target = args.recording / "impact-opening-configuration.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Opening impact factors saved at block {payload['block_number']}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
