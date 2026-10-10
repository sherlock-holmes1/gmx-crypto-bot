"""Capture and replay every pinned Step 4.1 decision transcript at one sidecar pin.

The pin is never chosen here: it is read from a saved archive sidecar whose own
values are first re-derived from its raw transcript. Each captured record is
then handed straight to the adapter's verifier, which recomputes its storage
keys, rebuilds its calldata and decodes its ABI results. Only a record its own
verifier can rebuild is written.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable

from gmx_crypto_bot_v2.application.router_check import HttpRpc
from gmx_crypto_bot_v2.crosscheck import pinned_capture as capture
from gmx_crypto_bot_v2.crosscheck.decision_adapter import (
    verify_pinned_balance_inputs, verify_pinned_borrowing_skip, verify_pinned_decision_config,
    verify_pinned_fee_clocks, verify_pinned_funding_selector, verify_pinned_increase_executor,
    verify_pinned_risk_cells, verify_pinned_swap_handler,
)
from gmx_crypto_bot_v2.crosscheck.sidecar_replay import verify_sidecar_transcript_values
from gmx_crypto_bot_v2.evidence.publication import atomic_json


RECORDS = ("increase-executor", "swap-handler", "decision-config", "risk-cells",
           "balance-inputs", "fee-clocks", "borrowing-skip", "funding-selector")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture pinned GMX decision transcripts at a saved sidecar's pin")
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--increase-executor-source", type=Path, required=True)
    parser.add_argument("--swap-handler-source", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True,
                        help="path prefix; each record is written as <prefix><name>.json")
    parser.add_argument("--record", action="append", choices=RECORDS, default=[],
                        help="capture only these records; default is all of them")
    args = parser.parse_args(argv)
    try:
        url = os.environ.get("GMX_ARCHIVE_RPC_URL")
        if not url:
            raise ValueError("GMX_ARCHIVE_RPC_URL is required")
        sidecar = json.loads(args.sidecar.read_text(encoding="utf-8"))
        replay = verify_sidecar_transcript_values(sidecar)
        pin, deployment = sidecar["pin"], sidecar["deployment"]
        block, block_hash = pin["number"], pin["recorded_hash"]
        datastore = deployment["datastore"]
        chain_id = deployment["chain_id"]
        order_handler = deployment["order_handler"]
        market = replay["market"]
        market_token = market["MARKET_TOKEN"]
        if market_token.lower() != str(sidecar["pinned_order"]["market"]).lower():
            raise ValueError("sidecar market token is not the pinned order's market")
        pinned = {"chain_id": chain_id, "block": block, "block_hash": block_hash}
        cells = {**pinned, "datastore": datastore}
        plans: dict[str, tuple[Callable[[], dict[str, Any]], Callable[[Path], Any]]] = {
            "increase-executor": (
                lambda: capture.capture_increase_executor(
                    HttpRpc(url), order_handler=order_handler, **pinned),
                lambda path: verify_pinned_increase_executor(
                    path, args.increase_executor_source)),
            "swap-handler": (
                lambda: capture.capture_swap_handler(
                    HttpRpc(url), order_handler=order_handler, **pinned),
                lambda path: verify_pinned_swap_handler(path, args.swap_handler_source)),
            "decision-config": (
                lambda: capture.capture_decision_config(
                    HttpRpc(url), market=sidecar["pinned_order"]["market"], **cells),
                lambda path: verify_pinned_decision_config(
                    path, args.increase_executor_source, sidecar)),
            "risk-cells": (
                lambda: capture.capture_risk_cells(
                    HttpRpc(url), market_token=market_token,
                    long_token=market["LONG_TOKEN"], short_token=market["SHORT_TOKEN"],
                    **cells),
                lambda path: verify_pinned_risk_cells(
                    path, args.increase_executor_source, sidecar)),
            "balance-inputs": (
                lambda: capture.capture_balance_inputs(
                    HttpRpc(url), market_token=market_token,
                    long_token=market["LONG_TOKEN"], short_token=market["SHORT_TOKEN"],
                    **cells),
                lambda path: verify_pinned_balance_inputs(
                    path, args.increase_executor_source, sidecar)),
            "fee-clocks": (
                lambda: capture.capture_fee_clocks(
                    HttpRpc(url), market=sidecar["pinned_order"]["market"], **cells),
                lambda path: verify_pinned_fee_clocks(
                    path, args.increase_executor_source, sidecar)),
            "borrowing-skip": (
                lambda: capture.capture_borrowing_skip(HttpRpc(url), **cells),
                lambda path: verify_pinned_borrowing_skip(
                    path, args.increase_executor_source, sidecar)),
            "funding-selector": (
                lambda: capture.capture_funding_selector(
                    HttpRpc(url), market=sidecar["pinned_order"]["market"], **cells),
                lambda path: verify_pinned_funding_selector(
                    path, args.increase_executor_source, sidecar)),
        }
        wanted = args.record or list(RECORDS)
        args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        for name in wanted:
            build, check = plans[name]
            record = build()
            if not capture.endpoint_free(record):
                raise ValueError(f"{name} capture would record an endpoint")
            path = Path(str(args.output_prefix) + name + ".json")
            atomic_json(path, record)
            try:
                check(path)
            except (ValueError, KeyError, TypeError, IndexError) as failure:
                path.unlink(missing_ok=True)
                raise ValueError(f"{name} capture failed its own verifier: {failure}") from failure
            print(f"Pinned {name}: {path}; block {block}")
    except (OSError, ValueError, KeyError, TypeError) as failure:
        parser.exit(2, f"gmx-pinned-capture: {failure}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
