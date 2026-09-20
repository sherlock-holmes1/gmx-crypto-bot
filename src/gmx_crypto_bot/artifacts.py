"""Append-only compressed raw source bundles kept outside replay event streams."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO


DEFAULT_MAX_BUNDLE_BYTES = 256 * 1024 * 1024


class RawArtifactStore:
    """Preserve exact public response bodies in rotated compressed JSONL bundles."""

    def __init__(self, recording_directory: Path, *, max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES) -> None:
        if max_bundle_bytes < 1:
            raise ValueError("max_bundle_bytes must be positive")
        self.directory = recording_directory / "raw"
        self.directory.mkdir(exist_ok=False)
        self._manifest = (self.directory / "manifest.jsonl").open("x", encoding="utf-8")
        self._max_bundle_bytes = max_bundle_bytes
        self._bundle: BinaryIO | None = None
        self._bundle_name: str | None = None
        self._bundle_bytes = 0
        self._bundle_record = 0
        self._bundle_sequence = 0
        self._sequence = 0
        self._closed = False

    def response(self, source: str, request: dict[str, Any], body: bytes) -> str:
        self._ensure_open()
        self._sequence += 1
        body_sha256 = hashlib.sha256(body).hexdigest()
        bundle_record = {
            "seq": self._sequence,
            "body_base64": base64.b64encode(body).decode("ascii"),
            "body_bytes": len(body),
            "body_sha256": body_sha256,
        }
        encoded_record = (json.dumps(bundle_record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        self._rotate_if_needed(len(encoded_record))
        assert self._bundle is not None
        assert self._bundle_name is not None
        self._bundle_record += 1
        self._bundle.write(encoded_record)
        self._bundle.flush()
        self._bundle_bytes += len(encoded_record)
        artifact = f"raw/{self._bundle_name}#{self._bundle_record}"
        self._write_manifest(
            {
                "seq": self._sequence,
                "kind": "response",
                "source": source,
                "request": request,
                "artifact": artifact,
                "bundle": f"raw/{self._bundle_name}",
                "bundle_record": self._bundle_record,
                "sha256": body_sha256,
                "bytes": len(body),
            }
        )
        return artifact

    def error(self, source: str, request: dict[str, Any], error: str) -> None:
        self._ensure_open()
        self._sequence += 1
        self._write_manifest(
            {"seq": self._sequence, "kind": "error", "source": source, "request": request, "error": error}
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._bundle is not None:
            self._bundle.close()
        self._manifest.close()
        self._closed = True

    def _rotate_if_needed(self, record_bytes: int) -> None:
        if self._bundle is not None and self._bundle_bytes + record_bytes <= self._max_bundle_bytes:
            return
        if self._bundle is not None:
            self._bundle.close()
        self._bundle_sequence += 1
        self._bundle_name = f"rpc-{self._bundle_sequence:06d}.jsonl.gz"
        self._bundle = gzip.open(self.directory / self._bundle_name, "xb")
        self._bundle_bytes = 0
        self._bundle_record = 0

    def _write_manifest(self, entry: dict[str, Any]) -> None:
        entry["received_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._manifest.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")
        self._manifest.flush()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("raw artifact store is closed")
