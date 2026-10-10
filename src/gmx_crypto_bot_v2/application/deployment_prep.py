"""Prepare observed historical deployment hashes for one archive sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from gmx_crypto_bot_v2.application.router_check import HttpRpc, _load_recorded_oracle_evidence
from gmx_crypto_bot_v2.crosscheck.boundary_pin import (
    pin_candidate_at_prestate_boundary, verified_boundary_proof)
from gmx_crypto_bot_v2.crosscheck.deployment_prep import prepare_deployment
from gmx_crypto_bot_v2.crosscheck.router_candidates import select_router_candidates
from gmx_crypto_bot_v2.crosscheck.router_preflight import _hex_bytes
from gmx_crypto_bot_v2.evidence.publication import atomic_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe pinned historical GMX deployment code")
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--order-key", required=True)
    parser.add_argument("--simulation-router", required=True,
                        help="explicit historical SimulationRouter address")
    parser.add_argument("--referral-storage",
                        help="explicit ReferralStorage address; checked against pinned OrderHandler pointer")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle-evidence", type=Path,
                        help="recorded observed-execution oracle evidence naming the execution")
    parser.add_argument("--prestate-proof", type=Path,
                        help="saved ArbOS system-transaction prestate proof")
    parser.add_argument("--pin-at-prestate-boundary", action="store_true",
                        help="observe the deployment at the block the prestate proof "
                             "established, instead of the order's creation block")
    args = parser.parse_args(argv)
    if args.pin_at_prestate_boundary and not (args.prestate_proof and args.oracle_evidence):
        parser.error("--pin-at-prestate-boundary requires a prestate proof and oracle evidence")
    try:
        url = os.environ.get("GMX_ARCHIVE_RPC_URL")
        if not url:
            raise ValueError("GMX_ARCHIVE_RPC_URL is required")
        key = "0x" + _hex_bytes(args.order_key, 32).hex()
        candidates = [row for row in select_router_candidates(args.recording)
                      if row.get("order_key") == key]
        if len(candidates) != 1:
            raise ValueError("expected exactly one recorded order key")
        spec_bytes = args.spec.read_bytes()
        metadata_path = args.recording / "metadata.json"
        metadata_bytes = metadata_path.read_bytes()
        spec = json.loads(spec_bytes)
        metadata = json.loads(metadata_bytes)
        spec_contracts = spec["contracts"]
        recorded = metadata["contracts"]
        for label in ("data_store", "order_handler"):
            if spec_contracts[label].lower() != recorded[label].lower():
                raise ValueError(f"spec and recording {label} differ")
        if spec["deployment"]["chain_id"] != metadata["market"]["chain_id"]:
            raise ValueError("spec and recording chain IDs differ")
        if spec["deployment"]["market_token_address"].lower() != \
                metadata["market"]["market_token_address"].lower():
            raise ValueError("spec and recording market differ")
        candidate = candidates[0]
        if args.pin_at_prestate_boundary:
            _, evidence = _load_recorded_oracle_evidence(args.oracle_evidence, args.recording)
            proof = verified_boundary_proof(args.prestate_proof, evidence,
                                            spec["deployment"]["chain_id"])
            candidate = pin_candidate_at_prestate_boundary(
                candidates, proof, evidence["order_key"])
            print(f"Deployment pin: block {candidate['proposed_pin_block']} "
                  f"({candidate['pin_source']})")
        result = prepare_deployment(
            HttpRpc(url), candidate, chain_id=spec["deployment"]["chain_id"],
            datastore=recorded["data_store"], reader=spec_contracts["reader"],
            order_handler=recorded["order_handler"], router=args.simulation_router,
            referral_storage=args.referral_storage)
        result["input_provenance"] = {
            "spec_path": str(args.spec),
            "spec_sha256": "0x" + hashlib.sha256(spec_bytes).hexdigest(),
            "recording_metadata_path": str(metadata_path),
            "recording_metadata_sha256": "0x" + hashlib.sha256(metadata_bytes).hexdigest(),
            "pin_source": candidate.get("pin_source", "recorded_creation_block"),
            "simulation_router_address_source": "explicit_cli_parameter",
            "referral_storage_address_source": (
                "explicit_cli_parameter" if args.referral_storage else None),
        }
        atomic_json(args.output, result)
        print(f"Observed deployment: {args.output}; source verified: false")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as failure:
        parser.exit(2, f"gmx-prepare-deployment: {failure}\n")


if __name__ == "__main__":
    raise SystemExit(main())
