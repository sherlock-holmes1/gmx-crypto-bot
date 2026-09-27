"""Lazy trace evidence supplied to checks; checks do not choose file paths."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TraceEvidence:
    captured: dict
    gas_probe: dict | None


class TraceEvidenceSource(Protocol):
    def get(self, transaction_hash: str) -> TraceEvidence | None: ...


class TraceRepository:
    def __init__(self, directory: Path):
        self.directory = directory / "execution-fee-traces"

    def get(self, transaction_hash: str) -> TraceEvidence | None:
        if not transaction_hash.startswith("0x") or any(
            c not in "0123456789abcdef" for c in transaction_hash[2:]
        ):
            raise ValueError("invalid trace transaction identifier")
        path = self.directory / (transaction_hash + ".json")
        if not path.exists():
            return None
        gas = self.directory / (transaction_hash + ".gas-v2.json")
        return TraceEvidence(
            json.loads(path.read_text()),
            json.loads(gas.read_text()) if gas.exists() else None,
        )
