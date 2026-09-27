"""Typed evidence shared by the dependency-ordered order checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gmx_crypto_bot_v2.evidence.traces import TraceEvidenceSource


@dataclass(frozen=True)
class ValidationContext:
    receipts: dict[str, dict[str, Any]]
    execution_fees: dict = field(default_factory=dict)
    update_topups: dict = field(default_factory=dict)
    opening_positions: dict = field(default_factory=dict)
    position_events: list = field(default_factory=list)
    swap_events: list = field(default_factory=list)
    payout_events: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    impact_factors: dict = field(default_factory=dict)
    fee_histories: dict = field(default_factory=dict)
    referral_state: Any = None
    modeled_swaps: list = field(default_factory=list)
    accrual_replay: Any = None
    traces: TraceEvidenceSource | None = None
    payout_receipt_available: bool = False
