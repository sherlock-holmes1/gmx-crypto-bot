"""Fixture adapter for legacy validator test cases."""

from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.checks.orders import validate_order
from gmx_crypto_bot_v2.reconstruction.accrual import AccrualReplay
from gmx_crypto_bot_v2.reconstruction.impact import HistoricalFactor
from gmx_crypto_bot_v2.reconstruction.referral import ReferralState


def _validate_order(
    key: str,
    request: dict[str, Any],
    lifecycle: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    receipts: dict[str, dict[str, Any]],
    execution_fees: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
    update_topups: dict[tuple[str, int], int] | None = None,
    opening_positions: dict[str, dict[str, Any]] | None = None,
    position_events: list[dict[str, Any]] | None = None,
    swap_events: list[dict[str, Any]] | None = None,
    payout_events: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    impact_factors: dict[str, HistoricalFactor] | None = None,
    fee_histories: dict | None = None,
    referral_state: ReferralState | None = None,
    modeled_swaps: list[dict[str, Any]] | None = None,
    accrual_replay: AccrualReplay | None = None,
    recording: Path | None = None,
    payout_receipt_available: bool = False,
):
    from gmx_crypto_bot_v2.domain.evidence import OrderContext
    from gmx_crypto_bot_v2.evidence.traces import TraceRepository
    from gmx_crypto_bot_v2.reconstruction.context import ValidationContext

    traces = TraceRepository(recording) if isinstance(recording, Path) else recording
    return validate_order(
        OrderContext(key, request, lifecycle, observed),
        ValidationContext(
            receipts=receipts,
            execution_fees=execution_fees,
            update_topups=update_topups,
            opening_positions=opening_positions,
            position_events=position_events,
            swap_events=swap_events,
            payout_events=payout_events,
            metadata=metadata,
            impact_factors=impact_factors,
            fee_histories=fee_histories,
            referral_state=referral_state,
            modeled_swaps=modeled_swaps,
            accrual_replay=accrual_replay,
            traces=traces,
            payout_receipt_available=payout_receipt_available,
        ),
    )
