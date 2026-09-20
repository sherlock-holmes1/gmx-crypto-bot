"""Deterministically order a GMX recording and verify its canonical digest."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from gmx_crypto_bot.recording import RecordedEvent, load_recording


@dataclass(frozen=True)
class ReplayReport:
    events: int
    canonical_events: int
    arrival_only_events: int
    first_block: int | None
    last_block: int | None
    digest: str


def canonical_events(events: list[RecordedEvent]) -> list[RecordedEvent]:
    return sorted(events, key=lambda event: event.canonical_key)


def replay(events: list[RecordedEvent]) -> ReplayReport:
    ordered = canonical_events(events)
    canonical = [event for event in ordered if event.block_number is not None]
    encoded = "\n".join(
        json.dumps(asdict(event), separators=(",", ":"), sort_keys=True) for event in ordered
    ).encode("utf-8")
    return ReplayReport(
        events=len(events),
        canonical_events=len(canonical),
        arrival_only_events=len(events) - len(canonical),
        first_block=None if not canonical else canonical[0].block_number,
        last_block=None if not canonical else canonical[-1].block_number,
        digest=hashlib.sha256(encoded).hexdigest(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify deterministic GMX recording replay.")
    parser.add_argument("recording", type=Path, help="Recording directory or events.jsonl path")
    parser.add_argument("--verify", action="store_true", help="Replay twice and compare digests")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text report")
    args = parser.parse_args()

    events, _metadata = load_recording(args.recording)
    report = replay(events)
    verified = None
    if args.verify:
        again, _ = load_recording(args.recording)
        verified = replay(again).digest == report.digest

    if args.json:
        result = asdict(report)
        if verified is not None:
            result["determinism_verified"] = verified
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Events      {report.events}")
        print(f"Canonical   {report.canonical_events}")
        print(f"Arrival-only {report.arrival_only_events}")
        print(f"Blocks      {report.first_block} → {report.last_block}")
        print(f"Digest      {report.digest}")
        if verified is not None:
            print(f"Determinism {'PASS — two replays agree' if verified else 'FAIL — replays disagree'}")
    return 0 if verified is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())

