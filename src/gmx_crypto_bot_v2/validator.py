"""Compatibility entry point; implementation lives in the layered V2 package."""

from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.application.validation import main as main
from gmx_crypto_bot_v2.application.validation import validate_orders as validate_orders
from gmx_crypto_bot_v2.checks.common import _comparison as _comparison
from gmx_crypto_bot_v2.checks.decrease import (
    _compare_collateral_conversion as _compare_collateral_conversion,
)
from gmx_crypto_bot_v2.checks.decrease import (
    _compare_decrease_settlement as _compare_decrease_settlement,
)
from gmx_crypto_bot_v2.checks.orders import _position_identity as _position_identity
from gmx_crypto_bot_v2.checks.orders import (
    _reconstruct_terminal_reason as _reconstruct_terminal_reason,
)
from gmx_crypto_bot_v2.checks.orders import (
    _request_position_identity as _request_position_identity,
)
from gmx_crypto_bot_v2.checks.orders import validate_order
from gmx_crypto_bot_v2.checks.position import _compare_execution as _compare_execution
from gmx_crypto_bot_v2.checks.position import (
    _compare_execution_price as _compare_execution_price,
)
from gmx_crypto_bot_v2.checks.position import (
    _compare_independent_price_impact as _compare_independent_price_impact,
)
from gmx_crypto_bot_v2.checks.position import (
    _compare_position_math as _compare_position_math,
)
from gmx_crypto_bot_v2.checks.position_fees import (
    _compare_execution_fee_events as _compare_execution_fee_events,
)
from gmx_crypto_bot_v2.checks.position_fees import (
    _compare_fee_math as _compare_fee_math,
)
from gmx_crypto_bot_v2.domain.constants import (
    ARBITRUM_MULTICHAIN_VAULT as ARBITRUM_MULTICHAIN_VAULT,
)
from gmx_crypto_bot_v2.domain.constants import (
    ARBITRUM_ORDER_VAULT as ARBITRUM_ORDER_VAULT,
)
from gmx_crypto_bot_v2.domain.constants import (
    DECREASE_ORDER_TYPES as DECREASE_ORDER_TYPES,
)
from gmx_crypto_bot_v2.domain.constants import (
    ERC20_TRANSFER_TOPIC as ERC20_TRANSFER_TOPIC,
)
from gmx_crypto_bot_v2.domain.constants import FLOAT_PRECISION as FLOAT_PRECISION
from gmx_crypto_bot_v2.domain.constants import FUNDING_PRECISION as FUNDING_PRECISION
from gmx_crypto_bot_v2.domain.constants import (
    IMPACT_ROUNDING_TOLERANCE_USD as IMPACT_ROUNDING_TOLERANCE_USD,
)
from gmx_crypto_bot_v2.domain.constants import (
    INCREASE_ORDER_TYPES as INCREASE_ORDER_TYPES,
)
from gmx_crypto_bot_v2.domain.constants import LIFECYCLE_EVENTS as LIFECYCLE_EVENTS
from gmx_crypto_bot_v2.domain.constants import MAX_UINT256 as MAX_UINT256
from gmx_crypto_bot_v2.domain.constants import POSITION_EVENTS as POSITION_EVENTS
from gmx_crypto_bot_v2.domain.constants import TERMINAL_EVENTS as TERMINAL_EVENTS
from gmx_crypto_bot_v2.domain.constants import (
    UNMODELED_ECONOMICS as UNMODELED_ECONOMICS,
)
from gmx_crypto_bot_v2.domain.entries import _coordinate as _coordinate
from gmx_crypto_bot_v2.domain.entries import (
    _decode_recorded_log as _decode_recorded_log,
)
from gmx_crypto_bot_v2.domain.entries import _event_entry as _event_entry
from gmx_crypto_bot_v2.domain.entries import (
    _event_entry_from_log as _event_entry_from_log,
)
from gmx_crypto_bot_v2.domain.entries import _order_key as _order_key
from gmx_crypto_bot_v2.models.arithmetic import _ceil_div as _ceil_div
from gmx_crypto_bot_v2.models.arithmetic import (
    _proportional_pending_impact as _proportional_pending_impact,
)
from gmx_crypto_bot_v2.models.arithmetic import _trunc_div as _trunc_div
from gmx_crypto_bot_v2.models.decrease import (
    _model_decrease_settlement as _model_decrease_settlement,
)
from gmx_crypto_bot_v2.reconstruction.accrual import AccrualReplay
from gmx_crypto_bot_v2.reconstruction.impact import (
    HistoricalFactor,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _associate_execution_fees as _associate_execution_fees,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _iter_raw_event_emitter_logs as _iter_raw_event_emitter_logs,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _load_execution_receipt_evidence as _load_execution_receipt_evidence,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _load_order_update_topups as _load_order_update_topups,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _load_replay_evidence as _load_replay_evidence,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _load_terminal_lifecycle as _load_terminal_lifecycle,
)
from gmx_crypto_bot_v2.reconstruction.orders import (
    _single_update_topup as _single_update_topup,
)
from gmx_crypto_bot_v2.reconstruction.positions import (
    _attach_pre_impact_pool as _attach_pre_impact_pool,
)
from gmx_crypto_bot_v2.reconstruction.positions import (
    _attach_pre_position_state as _attach_pre_position_state,
)
from gmx_crypto_bot_v2.reconstruction.positions import (
    _attach_pre_virtual_inventory as _attach_pre_virtual_inventory,
)
from gmx_crypto_bot_v2.reconstruction.referral import ReferralState
from gmx_crypto_bot_v2.reporting.orders import ValidationReport as ValidationReport
from gmx_crypto_bot_v2.reporting.orders import _order_result as _order_result


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


if __name__ == "__main__":
    raise SystemExit(main())
