"""CLI for the unified, read-only collection pipeline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from gmx_crypto_bot_v2.collection.base import DEFAULT_CHUNK_SIZE, DEFAULT_CONFIRMATIONS
from gmx_crypto_bot_v2.collection.coordinator import CollectionCoordinator
from gmx_crypto_bot_v2.sources.rpc import SourceError


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--spec", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--resume", action="store_true")
    result.add_argument("--rpc-url", default=os.environ.get("GMX_LOGS_RPC_URL"))
    result.add_argument(
        "--archive-rpc-url", default=os.environ.get("GMX_ARCHIVE_RPC_URL")
    )
    result.add_argument(
        "--last-days",
        type=int,
        help="Collect the last N days ending at the confirmed head; resume keeps the original window",
    )
    result.add_argument("--from-block", type=int)
    result.add_argument("--to-block", type=int)
    result.add_argument("--confirmations", type=int, default=DEFAULT_CONFIRMATIONS)
    result.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    result.add_argument("--timeout-seconds", type=int, default=90)
    result.add_argument("--gas-probes", action="store_true")
    result.add_argument("--cache-dir", type=Path)
    return result


def run(args, stages=None):
    spec = json.loads(args.spec.read_text()) if args.spec else None
    coordinator = CollectionCoordinator(
        args.output,
        spec=spec,
        rpc_url=args.rpc_url,
        archive_rpc_url=args.archive_rpc_url,
        resume=args.resume,
        last_days=getattr(args, "last_days", None),
        from_block=args.from_block,
        to_block=args.to_block,
        confirmations=args.confirmations,
        chunk_size=args.chunk_size,
        timeout_seconds=args.timeout_seconds,
        gas_probes=args.gas_probes,
        cache_directory=args.cache_dir,
    )
    try:
        result = coordinator.run(stages)
    except SourceError as error:
        print(
            f"Source request failed ({type(error).__name__}); completed work is retained for --resume."
        )
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        # Provider error text can contain a credential-bearing endpoint.
        print(f"Collection stopped: {error}. Completed work is retained for --resume.")
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["ready"] else 2


def main():
    command = parser()
    args = command.parse_args()
    if args.chunk_size <= 0 or args.confirmations < 0 or args.timeout_seconds <= 0:
        command.error(
            "chunk size and timeout must be positive; confirmations must be nonnegative"
        )
    if args.last_days is not None:
        if args.last_days <= 0:
            command.error("--last-days must be positive")
        if args.from_block is not None or args.to_block is not None:
            command.error("--last-days cannot be combined with explicit block bounds")
        if not args.resume and not args.spec:
            command.error("--last-days requires --spec for a fresh collection")
    if not args.resume and not args.archive_rpc_url:
        command.error(
            "fresh collection requires GMX_ARCHIVE_RPC_URL or --archive-rpc-url"
        )
    return run(args)


def stage_main(stage):
    command = argparse.ArgumentParser(
        description=f"Compatibility entry point for the unified {stage} collection stage"
    )
    command.add_argument("recording", type=Path)
    command.add_argument("--gas-probes", action="store_true")
    command.add_argument(
        "--include-liquidations", action="store_true", help="Always included in V2"
    )
    command.add_argument(
        "--limit", type=int, default=0, help="Only full coverage (0) is supported"
    )
    args = command.parse_args()
    if args.limit:
        command.error("V2 collects full trace coverage; use --limit 0")
    options = parser().parse_args(
        ["--output", str(args.recording), "--resume"]
        + (["--gas-probes"] if args.gas_probes else [])
    )
    return run(options, [stage])
