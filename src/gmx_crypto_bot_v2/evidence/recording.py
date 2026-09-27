"""Legacy-compatible normalized recording envelopes and append-only capture."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RecordedEvent:
    """One raw event with enough coordinates for canonical chain replay."""

    seq: int
    kind: str
    received_at: str
    received_monotonic_ns: int
    block_number: int | None
    transaction_index: int | None
    log_index: int | None
    payload: dict[str, Any]

    @property
    def canonical_key(self) -> tuple[int, int, int, int, int]:
        """Sort canonical chain events first and preserve arrival order otherwise."""
        if None not in (self.block_number, self.transaction_index, self.log_index):
            return (
                0,
                int(self.block_number),
                int(self.transaction_index),
                int(self.log_index),
                self.seq,
            )
        return (1, 0, 0, 0, self.seq)


class JsonlRecorder:
    """Write immutable metadata and flushed JSONL event envelopes."""

    def __init__(self, directory: Path, metadata: dict[str, Any]) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self._events_path = self.directory / "events.jsonl"
        (self.directory / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._stream = self._events_path.open("x", encoding="utf-8")
        self._seq = 0

    def record(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        block_number: int | None = None,
        transaction_index: int | None = None,
        log_index: int | None = None,
    ) -> RecordedEvent:
        self._seq += 1
        event = RecordedEvent(
            seq=self._seq,
            kind=kind,
            received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            received_monotonic_ns=time.monotonic_ns(),
            block_number=block_number,
            transaction_index=transaction_index,
            log_index=log_index,
            payload=payload,
        )
        self._stream.write(
            json.dumps(asdict(event), separators=(",", ":"), sort_keys=True) + "\n"
        )
        self._stream.flush()
        return event

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "JsonlRecorder":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def load_recording(path: Path) -> tuple[list[RecordedEvent], dict[str, Any]]:
    """Load a recording directory or its events file without changing it."""
    events_path = path / "events.jsonl" if path.is_dir() else path
    metadata_path = events_path.parent / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    rows = [
        RecordedEvent(**json.loads(line))
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    _validate_sequence(rows)
    return rows, metadata


def _validate_sequence(events: list[RecordedEvent]) -> None:
    expected = list(range(1, len(events) + 1))
    actual = [event.seq for event in events]
    if actual != expected:
        raise ValueError("recording event sequence is not contiguous from 1")
