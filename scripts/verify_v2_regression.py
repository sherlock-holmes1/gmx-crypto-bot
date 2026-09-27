#!/usr/bin/env python3
"""Verify a full offline V2 run against a saved V1 report; record cache and resource use."""

from __future__ import annotations

import argparse
import json
import resource
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from gmx_crypto_bot_v2.application.validation import _validate_orders
from gmx_crypto_bot_v2.evidence.repository import evidence_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    with patch(
        "socket.socket",
        side_effect=AssertionError("offline validation attempted network access"),
    ):
        with evidence_session(args.recording, args.cache_dir) as repository:
            report = json.loads(json.dumps(asdict(_validate_orders(args.recording))))
            stats = dict(repository.catalog.stats)
    elapsed = time.perf_counter() - started
    baseline = json.loads(args.baseline.read_text())
    differences = [
        key
        for key in baseline.keys() | report.keys()
        if baseline.get(key) != report.get(key)
    ]
    result = {
        "identical": not differences,
        "different_fields": differences,
        "elapsed_seconds": elapsed,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "catalog": stats,
        "matched": report["matched"],
        "mismatched": report["mismatched"],
        "decode_errors": report["decode_errors"],
        "remaining_economic_checks": report["remaining_economic_checks"],
        "complete": report["complete"],
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["identical"] and result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
