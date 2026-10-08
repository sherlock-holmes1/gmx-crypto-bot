"""Read-only CLI for recorded GMX latest-order preflight evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.crosscheck.router_candidates import select_router_candidates
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, OracleInput, RouterPreflight
from gmx_crypto_bot_v2.crosscheck.router_report import build_report
from gmx_crypto_bot_v2.crosscheck.router_watch import watch_order_creations


class HttpRpc:
    def __init__(self, url: str):
        self.url = url

    def request(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method not in {"eth_call", "eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_getLogs"}:
            raise ValueError("read-only RPC method required")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(self.url, data=payload,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return {"error": {"message": type(error).__name__}}


def _load_oracles(path: Path | None) -> dict[str, OracleInput]:
    if path is None:
        return {}
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("oracle input must map order keys to input objects")
    return {key.lower(): OracleInput(
        tuple(value["tokens"]), tuple(tuple(pair) for pair in value["prices"]),
        value["min_timestamp"], value["max_timestamp"], value["provenance"], value["scale_proof"])
        for key, value in values.items()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "0x" + digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check recorded GMX orders through read-only SimulationRouter calls")
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, help="verified historical router/DataStore deployment JSON")
    parser.add_argument("--oracles", type=Path, help="verified integer oracle input JSON, keyed by order key")
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--watch-start-block", type=int)
    parser.add_argument("--watch-end-block", type=int)
    args = parser.parse_args(argv)
    if args.max_candidates < 1:
        parser.error("--max-candidates must be positive")
    if (args.watch_start_block is None) != (args.watch_end_block is None):
        parser.error("both watch block bounds are required")
    if args.watch_start_block is not None and (args.watch_start_block < 0 or
            args.watch_end_block < args.watch_start_block or
            args.watch_end_block - args.watch_start_block > 7200):
        parser.error("watch range must contain at most 7201 blocks")
    try:
        metadata = args.recording / "metadata.json"
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        oracle = _load_oracles(args.oracles)
        url = os.environ.get("GMX_ARCHIVE_RPC_URL")
        if args.watch_start_block is not None:
            logs_url = os.environ.get("GMX_LOGS_RPC_URL") or url
            if not logs_url:
                raise ValueError("watch requires GMX_LOGS_RPC_URL or GMX_ARCHIVE_RPC_URL")
            emitter = metadata_value["contracts"]["event_emitter"]
            market = metadata_value["market"]["market_token_address"]
            candidates = watch_order_creations(HttpRpc(logs_url), emitter,
                                                args.watch_start_block, args.watch_end_block,
                                                market)[:args.max_candidates]
            source = "watch"
        else:
            candidates = select_router_candidates(args.recording)[:args.max_candidates]
            source = "historical"
        deployment = Deployment(**json.loads(args.deployment.read_text(encoding="utf-8"))) if args.deployment else None
        preflight = RouterPreflight(HttpRpc(url), deployment) if url and deployment else None
        report = build_report(candidates, preflight, oracle, source=source, recording=str(args.recording))
        report["run"] = {
            "utc_run_time": datetime.now(timezone.utc).isoformat(),
            "recording_events_sha256": _sha256(args.recording / "events.jsonl"),
            "recording_metadata_sha256": _sha256(metadata),
            "spec_sha256": metadata_value.get("spec_sha256"),
            "chain_id": deployment.chain_id if deployment else None,
            "router_address": deployment.router if deployment else None,
            "router_code_hash": deployment.router_code_hash if deployment else None,
            "abi_identity": "simulateExecuteLatestOrder((address[],(uint256,uint256)[],uint256,uint256))",
            "rpc_capability": "archive_endpoint_configured_unverified" if url else "archive_endpoint_missing",
            "watch_range": ([args.watch_start_block, args.watch_end_block]
                            if args.watch_start_block is not None else None),
            "watch_status": "collected" if args.watch_start_block is not None else None,
        }
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"gmx-router-check: {error}\n")
    print(f"Report: {args.output}; candidates: {len(report['candidates'])}; verified comparisons: "
          f"{sum(r['comparison']['status'] != 'gmx_preflight_only' for r in report['candidates'])}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
