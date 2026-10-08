"""Build an offline historical-source lead from a saved archive sidecar."""

from __future__ import annotations

import argparse
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.source_manifest import build_source_manifest
from gmx_crypto_bot_v2.evidence.publication import atomic_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract pinned Solidity metadata references")
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_source_manifest(args.sidecar)
        atomic_json(args.output, result)
    except (OSError, ValueError, KeyError, TypeError) as failure:
        parser.exit(2, f"gmx-source-manifest: {failure}\n")
    print(f"Source manifest: {args.output}; historical source proved: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
