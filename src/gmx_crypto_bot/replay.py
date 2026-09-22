"""Deterministically order a GMX recording and reconstruct observable state."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from gmx_crypto_bot.recording import RecordedEvent, load_recording
from gmx_crypto_bot.state import build_state, report_from_state


@dataclass(frozen=True)
class ReplayReport:
    events: int
    canonical_events: int
    arrival_only_events: int
    first_block: int | None
    last_block: int | None
    digest: str
    event_digest: str
    state_complete: bool | None


def canonical_events(events: list[RecordedEvent]) -> list[RecordedEvent]:
    return sorted(events, key=lambda event: event.canonical_key)


def _replay_with_state(
    events: list[RecordedEvent], metadata: dict[str, object] | None = None
) -> tuple[ReplayReport, object | None]:
    ordered = canonical_events(events)
    canonical = [
        event
        for event in ordered
        if None not in (event.block_number, event.transaction_index, event.log_index)
    ]
    encoded = "\n".join(
        json.dumps(asdict(event), separators=(",", ":"), sort_keys=True) for event in ordered
    ).encode("utf-8")
    event_digest = hashlib.sha256(encoded).hexdigest()
    if metadata is None:
        digest, state_complete = event_digest, None
    else:
        state = build_state(ordered, metadata)
        state_encoded = json.dumps(state.as_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        digest, state_complete = hashlib.sha256(state_encoded).hexdigest(), state.complete
    return ReplayReport(
        events=len(events),
        canonical_events=len(canonical),
        arrival_only_events=len(events) - len(canonical),
        first_block=None if not canonical else canonical[0].block_number,
        last_block=None if not canonical else canonical[-1].block_number,
        digest=digest,
        event_digest=event_digest,
        state_complete=state_complete,
    ), state if metadata is not None else None


def replay(events: list[RecordedEvent], metadata: dict[str, object] | None = None) -> ReplayReport:
    """Return deterministic ordering and state-digest verification metadata."""
    report, _state = _replay_with_state(events, metadata)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify deterministic GMX recording replay.")
    parser.add_argument("recording", type=Path, help="Recording directory or events.jsonl path")
    parser.add_argument("--verify", action="store_true", help="Replay twice and compare digests")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text report")
    parser.add_argument("--output", type=Path, help="Write the full reconstructed-state JSON report")
    parser.add_argument("--spec", type=Path, help="Supply token metadata for an older recording without changing it")
    parser.add_argument("--opening-checkpoint", type=Path, help="Block-pinned observable state before the recording window")
    args = parser.parse_args()

    events, metadata = load_recording(args.recording)
    metadata_overrides: dict[str, object] = {}
    if args.spec:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        if spec["deployment"]["market_token_address"].lower() != metadata["market"]["market_token_address"].lower():
            parser.error("spec market does not match recording market")
        metadata_overrides["tokens"] = spec["tokens"]
    if args.opening_checkpoint:
        checkpoint = json.loads(args.opening_checkpoint.read_text(encoding="utf-8"))
        first_block = min(event.block_number for event in events if event.block_number is not None)
        if checkpoint.get("block_number") is None or int(checkpoint["block_number"]) >= first_block:
            parser.error("opening checkpoint block must precede the first recorded block")
        metadata_overrides["opening_state_checkpoint"] = checkpoint
    metadata = {**metadata, **metadata_overrides}
    report, state = _replay_with_state(events, metadata)
    if state is None:
        raise RuntimeError("recording metadata is required for state reconstruction")
    state_report = report_from_state(state)
    verified = None
    if args.verify:
        again, again_metadata = load_recording(args.recording)
        again_metadata = {**again_metadata, **metadata_overrides}
        verified = replay(again, again_metadata).digest == report.digest

    if args.output:
        args.output.write_text(json.dumps(state_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        result = asdict(report)
        if verified is not None:
            result["determinism_verified"] = verified
        result["orders"] = state_report["orders"]
        result["market"] = state_report["market"]
        result["data_quality"] = state_report["data_quality"]
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Events      {report.events}")
        print(f"Canonical   {report.canonical_events}")
        print(f"Arrival-only {report.arrival_only_events}")
        print(f"Blocks      {report.first_block} → {report.last_block}")
        print(f"Event digest {report.event_digest}")
        print(f"State digest {report.digest}")
        print(f"State       {'COMPLETE' if report.state_complete else 'INCOMPLETE'}")
        print(f"Orders      {state_report['orders']['created']} created, {state_report['orders']['unresolved']} unresolved")
        print(f"Terminal    {state_report['orders']['terminal_outcomes']}")
        print(f"Data gaps   {state_report['data_quality']['gaps']} · Reorgs {state_report['data_quality']['reorgs']}")
        if verified is not None:
            print(f"Determinism {'PASS — two replays agree' if verified else 'FAIL — replays disagree'}")
    return 0 if verified is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
