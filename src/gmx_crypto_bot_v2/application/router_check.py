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
from gmx_crypto_bot_v2.crosscheck.oracle_evidence import (
    capture_recorded_oracle, capture_watched_creation_oracle)
from gmx_crypto_bot_v2.crosscheck.decision_adapter import SelectedIncreaseDecisionAdapter
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, OracleInput, RouterPreflight, encode_simulation
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


def _load_recorded_oracle_evidence(path: Path, recording: Path) -> tuple[dict[str, OracleInput], dict[str, Any]]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if evidence.get("schema") != "GmxStep41RecordedOracleEvidence" or \
            evidence.get("version") != 1 or \
            not evidence.get("source_and_scale_verified") or \
            evidence.get("ready_for_observed_execution_comparison") is not False or \
            evidence.get("relationship") not in {
                "order_transaction_oracle_before_creation_not_execution_proof",
                "observed_execution_transaction_prices_preceding_order_executed",
                "counterfactual_other_transaction"}:
        raise ValueError("oracle evidence is unverified or lacks counterfactual classification")
    key = evidence.get("order_key")
    if not isinstance(key, str) or len(key) != 66 or not key.startswith("0x"):
        raise ValueError("oracle evidence order key missing")
    oracle = OracleInput(tuple(evidence["tokens"]),
                         tuple(tuple(pair) for pair in evidence["prices"]),
                         evidence["min_timestamp"], evidence["max_timestamp"],
                         evidence["provenance"], evidence["scale_proof"])
    if evidence.get("router_calldata") != encode_simulation(oracle):
        raise ValueError("oracle evidence calldata mismatch")
    rebuilt = capture_recorded_oracle(
        recording, evidence["oracle_transaction_hash"], oracle.tokens,
        order_key=key, order_transaction_hash=evidence.get("order_transaction_hash"),
        order_event_name=evidence.get("order_event_name", "OrderCreated"),
        source_record=path.parent / "sourcify-v2/oracle.json",
        pinned_code_proof=path.parent / (
            "execution-oracle-code.json" if evidence.get("order_event_name") == "OrderExecuted"
            else "pinned-oracle-code.json"))
    if rebuilt != evidence:
        raise ValueError("oracle evidence differs from recorded logs or historical source proof")
    return {key.lower(): oracle}, evidence


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
    parser.add_argument("--oracle-evidence", type=Path,
                        help="one recorded, source-proved oracle input with a counterfactual label")
    parser.add_argument("--archive-sidecar", type=Path,
                        help="pinned selected-order archive state for independent decision gate")
    parser.add_argument("--source-proof", type=Path,
                        help="digest-linked Sourcify source proof")
    parser.add_argument("--source-manifest", type=Path,
                        help="historical source manifest bound to archive sidecar")
    parser.add_argument("--increase-executor-proof", type=Path,
                        help="pinned increase executor pointer and code transcript")
    parser.add_argument("--increase-executor-source", type=Path,
                        help="exact-match Sourcify source for increase executor")
    parser.add_argument("--swap-handler-proof", type=Path,
                        help="pinned swap handler pointer and code transcript")
    parser.add_argument("--swap-handler-source", type=Path,
                        help="exact-match Sourcify source for swap handler")
    parser.add_argument("--watch-oracle-source", type=Path,
                        help="saved Sourcify exact-match Oracle record for watched creation prices")
    parser.add_argument("--watch-oracle-address", help="Oracle deployment address to verify at each watched block")
    parser.add_argument("--watch-oracle-token", action="append", default=[],
                        help="required oracle token in router order; supply twice")
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--watch-start-block", type=int)
    parser.add_argument("--watch-end-block", type=int)
    args = parser.parse_args(argv)
    if args.max_candidates < 1:
        parser.error("--max-candidates must be positive")
    if args.oracles and args.oracle_evidence:
        parser.error("--oracles and --oracle-evidence are mutually exclusive")
    if args.watch_oracle_source and (args.oracles or args.oracle_evidence):
        parser.error("watch oracle capture cannot be mixed with saved oracle inputs")
    decision_paths = (args.archive_sidecar, args.source_proof, args.source_manifest,
                      args.increase_executor_proof, args.increase_executor_source,
                      args.swap_handler_proof, args.swap_handler_source)
    if any(decision_paths) and not (all(decision_paths) and args.oracle_evidence):
        parser.error("independent decision gate requires sidecar, source proof, manifest, and oracle evidence")
    if any((args.watch_oracle_source, args.watch_oracle_address, args.watch_oracle_token)) and \
            not (args.watch_oracle_source and args.watch_oracle_address and
                 len(args.watch_oracle_token) == 2 and args.watch_start_block is not None):
        parser.error("watch oracle capture requires watch bounds, source, address, and two tokens")
    if (args.watch_start_block is None) != (args.watch_end_block is None):
        parser.error("both watch block bounds are required")
    if args.watch_start_block is not None and (args.watch_start_block < 0 or
            args.watch_end_block < args.watch_start_block or
            args.watch_end_block - args.watch_start_block > 7200):
        parser.error("watch range must contain at most 7201 blocks")
    try:
        metadata = args.recording / "metadata.json"
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        oracle, recorded_oracle_evidence = (_load_recorded_oracle_evidence(args.oracle_evidence, args.recording)
                                             if args.oracle_evidence else (_load_oracles(args.oracles), None))
        url = os.environ.get("GMX_ARCHIVE_RPC_URL")
        watched_oracle_evidence = {}
        if args.watch_start_block is not None:
            logs_url = os.environ.get("GMX_LOGS_RPC_URL") or url
            if not logs_url:
                raise ValueError("watch requires GMX_LOGS_RPC_URL or GMX_ARCHIVE_RPC_URL")
            emitter = metadata_value["contracts"]["event_emitter"]
            market = metadata_value["market"]["market_token_address"]
            candidates = watch_order_creations(HttpRpc(logs_url), emitter,
                                                args.watch_start_block, args.watch_end_block,
                                                market)[:args.max_candidates]
            if args.watch_oracle_source:
                for candidate in candidates:
                    if candidate["selection_skip_reasons"]:
                        continue
                    try:
                        captured = capture_watched_creation_oracle(
                            HttpRpc(logs_url), candidate, emitter, args.watch_oracle_address,
                            args.watch_oracle_source, tuple(args.watch_oracle_token))
                        key = candidate["order_key"]
                        oracle[key] = OracleInput(tuple(captured["tokens"]),
                                                  tuple(tuple(pair) for pair in captured["prices"]),
                                                  captured["min_timestamp"], captured["max_timestamp"],
                                                  "watched_OraclePriceUpdate_events",
                                                  "historical_Oracle_primary_price_raw_integer")
                        watched_oracle_evidence[key] = captured
                    except (ValueError, KeyError, TypeError) as error:
                        candidate["oracle_evidence_failure"] = str(error)
            source = "watch"
        else:
            candidates = select_router_candidates(args.recording)[:args.max_candidates]
            source = "historical"
        deployment = Deployment(**json.loads(args.deployment.read_text(encoding="utf-8"))) if args.deployment else None
        preflight = RouterPreflight(HttpRpc(url), deployment) if url and deployment else None
        decision_adapter = (SelectedIncreaseDecisionAdapter(
            args.archive_sidecar, args.source_proof, args.source_manifest, args.oracle_evidence,
            args.increase_executor_proof, args.increase_executor_source,
            args.swap_handler_proof, args.swap_handler_source)
                            if all(decision_paths) else None)
        report = build_report(candidates, preflight, oracle, source=source,
                              recording=str(args.recording), decision_adapter=decision_adapter)
        if recorded_oracle_evidence is not None:
            report["oracle_evidence"] = {
                "order_key": recorded_oracle_evidence["order_key"],
                "relationship": recorded_oracle_evidence["relationship"],
                "source_and_scale_verified": recorded_oracle_evidence["source_and_scale_verified"],
                "observed_execution_comparison_allowed": False,
                "evidence_sha256": _sha256(args.oracle_evidence),
            }
        if watched_oracle_evidence:
            report["watched_oracle_evidence"] = watched_oracle_evidence
            report["watched_oracle_relationship"] = "counterfactual_creation_prices_only"
            report["observed_execution_comparison_allowed"] = False
        elif args.oracles:
            report["oracle_evidence"] = {
                "source": "legacy_caller_supplied_input",
                "source_and_scale_verified": False,
                "observed_execution_comparison_allowed": False,
                "evidence_sha256": _sha256(args.oracles),
            }
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
