"""Immutable identities crossing the collection and validation boundaries."""

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, order=True)
class Coordinate:
    block: int
    transaction: int
    log: int


@dataclass(frozen=True)
class BlockReference:
    number: int
    hash: str


@dataclass(frozen=True)
class EvidenceReference:
    artifact: str
    sha256: str


@dataclass(frozen=True)
class TraceRequirement:
    transaction_hash: str
    block: BlockReference
    reasons: frozenset[str]


@dataclass(frozen=True)
class CollectionRequirements:
    order_keys: frozenset[str]
    traders: frozenset[str]
    markets: frozenset[str]
    ui_receivers: frozenset[str]
    traces: tuple[TraceRequirement, ...]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    expected: Any = None
    observed: Any = None
    discrepancy: Any = None
    evidence: tuple[EvidenceReference, ...] = ()


@dataclass(frozen=True)
class OrderContext:
    key: str
    request: Mapping[str, Any]
    lifecycle: tuple[Mapping[str, Any], ...]
    observed: tuple[Mapping[str, Any], ...]
