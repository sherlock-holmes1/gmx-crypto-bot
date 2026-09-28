"""Lazy trace evidence supplied to checks; checks do not choose file paths."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gmx_crypto_bot_v2.evidence.trace_store import TraceStore


@dataclass(frozen=True)
class TraceEvidence:
    captured: dict
    gas_probe: dict | None


class TraceEvidenceSource(Protocol):
    def get(self, transaction_hash: str) -> TraceEvidence | None: ...


class TraceRepository:
    def __init__(self, directory: Path):
        self.directory = directory / "execution-fee-traces"
        self.store = (
            TraceStore(directory, read_only=True)
            if (directory / ".collection/traces.sqlite").exists()
            else None
        )

    def get(self, transaction_hash: str) -> TraceEvidence | None:
        if not transaction_hash.startswith("0x") or any(
            c not in "0123456789abcdef" for c in transaction_hash[2:]
        ):
            raise ValueError("invalid trace transaction identifier")
        if self.store is not None:
            trace = self.store.get("trace", transaction_hash)
            if trace is not None:
                return TraceEvidence(trace, self.store.get("gas", transaction_hash))
        path = self.directory / (transaction_hash + ".json")
        if not path.exists():
            return None
        gas = self.directory / (transaction_hash + ".gas-v2.json")
        return TraceEvidence(
            json.loads(path.read_text()),
            json.loads(gas.read_text()) if gas.exists() else None,
        )
