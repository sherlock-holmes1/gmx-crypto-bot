"""Collection progress survives deletion of the disposable SQLite catalog."""

from __future__ import annotations

import json
from pathlib import Path

from gmx_crypto_bot_v2.evidence.catalog import digest
from gmx_crypto_bot_v2.evidence.publication import atomic_json
from gmx_crypto_bot_v2.evidence.trace_store import TraceStore, sha256


class CollectionJournal:
    def __init__(self, directory: Path, identity: dict):
        self.directory = directory
        self.path = directory / ".collection/journal.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state["identity"] != identity:
                raise ValueError("resume identity differs from original collection")
        else:
            self.state = {"version": 1, "identity": identity, "units": {}}
            self.save()

    def save(self):
        atomic_json(self.path, self.state)

    def completed(self, unit: str) -> bool:
        entry = self.state["units"].get(unit)
        if not entry:
            return False
        for name, expected in entry["artifacts"].items():
            if name.startswith("@trace-store/"):
                _, kind, transaction = name.split("/", 2)
                path = self.directory / ".collection/traces.sqlite"
                actual = (
                    None
                    if not path.exists()
                    else TraceStore(self.directory, read_only=True).get_bytes(
                        kind, transaction
                    )
                )
                if actual is None or sha256(actual) != expected:
                    raise ValueError(
                        f"stored trace evidence missing or corrupt: {transaction}"
                    )
            else:
                path = self.directory / name
                if not path.exists() or digest(path) != expected:
                    raise ValueError(f"published evidence missing or corrupt: {name}")
        return True

    def complete(self, unit: str, artifacts: list[Path]):
        self.state["units"][unit] = {
            "artifacts": {
                str(path.relative_to(self.directory)): digest(path)
                for path in artifacts
            }
        }
        self.save()

    def complete_store(self, unit: str, kind: str, transaction: str, digest_value: str):
        self.state["units"][unit] = {
            "artifacts": {f"@trace-store/{kind}/{transaction}": digest_value}
        }
        self.save()
