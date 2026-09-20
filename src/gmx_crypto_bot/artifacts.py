"""Append-only raw source artifacts kept outside replay event streams."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RawArtifactStore:
    """Preserve exact public response bodies with a request manifest."""

    def __init__(self, recording_directory: Path) -> None:
        self.directory = recording_directory / "raw"
        self.directory.mkdir(exist_ok=False)
        self._manifest = (self.directory / "manifest.jsonl").open("x", encoding="utf-8")
        self._sequence = 0

    def response(self, source: str, request: dict[str, Any], body: bytes) -> str:
        self._sequence += 1
        filename = f"{self._sequence:08d}-{_safe_name(source)}.json"
        path = self.directory / filename
        path.write_bytes(body)
        self._write_manifest(
            {
                "seq": self._sequence,
                "kind": "response",
                "source": source,
                "request": request,
                "path": f"raw/{filename}",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
        return f"raw/{filename}"

    def error(self, source: str, request: dict[str, Any], error: str) -> None:
        self._sequence += 1
        self._write_manifest(
            {"seq": self._sequence, "kind": "error", "source": source, "request": request, "error": error}
        )

    def close(self) -> None:
        self._manifest.close()

    def _write_manifest(self, entry: dict[str, Any]) -> None:
        entry["received_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._manifest.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")
        self._manifest.flush()


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in value).strip("-")
