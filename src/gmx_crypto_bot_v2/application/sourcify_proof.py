"""Verify saved public Sourcify responses against pinned Step 4.1 code."""

from __future__ import annotations

import argparse
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.sourcify_proof import verify_sourcify_records
from gmx_crypto_bot_v2.evidence.publication import atomic_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify public Sourcify exact-match records")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_sourcify_records(args.manifest, args.records)
        atomic_json(args.output, result)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as failure:
        parser.exit(2, f"gmx-sourcify-proof: {failure}\n")
    print(f"Sourcify proof: {args.output}; contracts: {len(result['contracts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
