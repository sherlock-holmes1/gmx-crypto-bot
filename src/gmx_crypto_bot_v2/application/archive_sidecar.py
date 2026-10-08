"""Collect one recorded GMX order's pinned archive evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from gmx_crypto_bot_v2.application.router_check import HttpRpc
from gmx_crypto_bot_v2.crosscheck.archive_sidecar import collect_increase_sidecar
from gmx_crypto_bot_v2.crosscheck.router_candidates import select_router_candidates
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, _hex_bytes
from gmx_crypto_bot_v2.evidence.publication import atomic_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect read-only pinned GMX archive evidence")
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--order-key", required=True)
    parser.add_argument("--deployment", type=Path, required=True,
                        help="verified historical router/DataStore/Reader deployment JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        url = os.environ.get("GMX_ARCHIVE_RPC_URL")
        if not url:
            raise ValueError("GMX_ARCHIVE_RPC_URL is required")
        key = "0x" + _hex_bytes(args.order_key, 32).hex()
        candidates = [row for row in select_router_candidates(args.recording)
                      if row.get("order_key") == key]
        if len(candidates) != 1:
            raise ValueError("expected exactly one recorded order key")
        deployment_bytes = args.deployment.read_bytes()
        configured = json.loads(deployment_bytes)
        deployment = Deployment(**configured["deployment"])
        reader = configured["reader"]
        _hex_bytes(reader["address"], 20)
        _hex_bytes(reader["code_hash"], 32)
        metadata = json.loads((args.recording / "metadata.json").read_text(encoding="utf-8"))
        if configured.get("schema") == "GmxStep41ObservedDeployment":
            observed = configured["candidate"]
            selected = candidates[0]
            if (observed["order_key"].lower() != key or
                    observed["pin_block"] != selected["proposed_pin_block"] or
                    observed["pin_hash"].lower() != selected["proposed_pin_hash"].lower()):
                raise ValueError("observed deployment belongs to a different candidate or pin")
        if metadata["market"]["chain_id"] != deployment.chain_id or \
                metadata["contracts"]["data_store"].lower() != deployment.datastore.lower():
            raise ValueError("deployment does not match recording metadata")
        tokens = metadata["tokens"]
        order_handler = configured.get("order_handler")
        referral = configured.get("referral_storage")
        if order_handler and order_handler["address"].lower() != metadata["contracts"]["order_handler"].lower():
            raise ValueError("order handler does not match recording metadata")
        result = collect_increase_sidecar(
            HttpRpc(url), deployment, candidates[0],
            reader_address=reader["address"], reader_code_hash=reader["code_hash"],
            index_token=tokens["index"]["address"],
            long_token=tokens["long"]["address"],
            short_token=tokens["short"]["address"],
            order_handler=order_handler["address"] if order_handler else None,
            order_handler_code_hash=order_handler["code_hash"] if order_handler else None,
            referral_storage=referral["address"] if referral else None,
            referral_storage_code_hash=referral["code_hash"] if referral else None)
        result["source"]["recording_metadata"] = {
            "chain_id": metadata["market"]["chain_id"],
            "market": metadata["market"]["market_token_address"],
            "data_store": metadata["contracts"]["data_store"]}
        result["deployment_provenance"] = {
            "input_sha256": "0x" + hashlib.sha256(deployment_bytes).hexdigest(),
            "proof_level": configured.get("proof_level", "user_supplied_unverified")}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output, result)
        print(f"Sidecar: {args.output}; status: {result['status']}; ready: false")
        return 0 if result["status"] == "partial_archive_snapshot" else 1
    except (OSError, ValueError, KeyError, TypeError) as failure:
        parser.exit(2, f"gmx-archive-sidecar: {failure}\n")


if __name__ == "__main__":
    raise SystemExit(main())
