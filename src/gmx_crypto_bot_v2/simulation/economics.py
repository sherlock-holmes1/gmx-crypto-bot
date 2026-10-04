"""Independent GMX position economics at one historical execution state.

Inputs are pre-order market evidence and a hypothetical position. Recorded fee,
impact and liquidation outputs are never used as model inputs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from gmx_crypto_bot_v2.domain.constants import (
    FLOAT_PRECISION, FUNDING_PRECISION, IMPACT_ROUNDING_TOLERANCE_USD,
)
from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.models.accrual import ceil_div
from gmx_crypto_bot_v2.models.arithmetic import _proportional_pending_impact, _trunc_div
from gmx_crypto_bot_v2.models.impact import predict_current_impact
from gmx_crypto_bot_v2.models.referral import discount_amounts
from gmx_crypto_bot_v2.simulation.pnl_cap import capped_position_pnl
from gmx_crypto_bot_v2.models.settlement import Cash
from gmx_crypto_bot_v2.simulation.evidence import EvidenceState, UnavailableEvidence

ZERO = "0x" + "0" * 40


@dataclass(frozen=True)
class PositionBefore:
    size_usd: int
    size_tokens: int
    collateral_amount: int
    borrowing_factor: int
    funding_fee_per_size: int
    pending_impact_amount: int = 0


@dataclass(frozen=True)
class ReferralTerms:
    code: str
    rebate_bps: int
    share_bps: int
    minimum: int
    pro_tier: int
    pro_factor: int


@dataclass(frozen=True)
class EconomicOrder:
    is_increase: bool
    is_long: bool
    size_delta_usd: int
    size_delta_tokens: int
    collateral_token: str
    acceptable_price: int | None
    virtual_inventory_tokens: int | None
    ui_fee_receiver: str = ZERO
    execution_fee_wei: int = 0
    withdrawal_amount: int = 0
    is_liquidation: bool = False
    collateral_delta_amount: int = 0


@dataclass(frozen=True)
class EconomicResult:
    status: str
    reason: str | None
    impact: Mapping[str, int | bool]
    fees: Mapping[str, int]
    execution_price: int | None
    acceptable_met: bool | None
    settlement: Mapping[str, Any] | None
    execution_fee_wei: int
    calculation_digest: str | None = None


def calculation_digest(
    state: EvidenceState, order: EconomicOrder, before: PositionBefore
) -> str:
    """Bind a candidate to the precise evidence overlay and position inputs."""
    payload = {
        "coordinate": state.coordinate,
        "transaction_hash": state.transaction_hash,
        "market": state.market,
        "tokens": (state.index_token, state.long_token, state.short_token),
        "oracle": state.oracle,
        "configuration": state.configuration,
        "accrual": state.accrual,
        "open_interest_usd": state.open_interest_usd,
        "open_interest_tokens": state.open_interest_tokens,
        "pool_amount": state.pool_amount,
        "impact_pool_amount": state.impact_pool_amount,
        "order": asdict(order),
        "before": asdict(before),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def compare_validated_order(
    modeled: EconomicResult, order_report: Mapping[str, Any]
) -> Mapping[str, str]:
    """Compare independent outputs to a validator order without reading them as inputs."""
    if modeled.status == "unavailable":
        return {"economics": "unavailable"}
    observed = order_report.get("observed_execution_events", ())
    positions = [e for e in observed if e.get("event_name") in
                 {"PositionIncrease", "PositionDecrease"}]
    fees = [e for e in observed if e.get("event_name") == "PositionFeesCollected"]
    if len(positions) != 1 or len(fees) != 1:
        return {"economics": "unavailable"}
    position = positions[0]["values"]
    charges = fees[0]["values"]
    impact_field = ("pendingPriceImpactUsd" if positions[0]["event_name"] == "PositionIncrease"
                    else "priceImpactUsd")
    expected = {
        "execution_price": (modeled.execution_price, position.get("executionPrice")),
        "current_impact_usd": (modeled.impact.get("current_impact_usd"),
                               position.get(impact_field)),
        "position_fee_amount": (modeled.fees.get("position_fee_amount"),
                                charges.get("positionFeeAmount")),
        "borrowing_fee_usd": (modeled.fees.get("borrowing_fee_usd"),
                              charges.get("borrowingFeeUsd")),
        "funding_fee_amount": (modeled.fees.get("funding_fee_amount"),
                               charges.get("fundingFeeAmount")),
        "total_cost_amount": (modeled.fees.get("total_cost_amount"),
                              charges.get("totalCostAmount")),
    }
    if positions[0]["event_name"] == "PositionDecrease":
        expected["total_impact_usd"] = (
            modeled.impact.get("total_impact_usd"), position.get("totalImpactUsd"))
        if modeled.settlement is not None:
            expected["base_pnl_usd"] = (
                modeled.settlement.get("base_pnl_usd"), position.get("basePnlUsd"))
    return {name: ("unavailable" if not all(type(v) is int for v in pair)
                   else "matched" if abs(pair[0] - pair[1]) <=
                   (IMPACT_ROUNDING_TOLERANCE_USD if "impact_usd" in name else 0)
                   else "mismatch")
            for name, pair in expected.items()}


def _required(value: Any, label: str) -> int:
    if type(value) is not int:
        raise UnavailableEvidence(f"missing {label}")
    return value


def _config(state: EvidenceState, group: str, name: str) -> int:
    return _required(state.configuration.get(group + ":" + name), name)


def _storage(state: EvidenceState, name: str, *args: Any) -> int:
    return _required(state.accrual.get(key(name, *args)), name)


def _settle_decrease(
    state: EvidenceState,
    order: EconomicOrder,
    before: PositionBefore,
    impact: int,
    impact_diff: int,
    funding: int,
    other_fees: int,
) -> Mapping[str, Any]:
    """Pay GMX's ordered costs, then release requested or closing collateral."""
    index = state.index_token
    index_min, index_max = state.oracle[index]
    collateral_min, _ = state.oracle[order.collateral_token]
    pnl_token = state.long_token if order.is_long else state.short_token
    pnl_min, pnl_max = state.oracle[pnl_token]
    total_pnl = before.size_tokens * (index_min if order.is_long else index_max) - before.size_usd
    if not order.is_long:
        total_pnl = -total_pnl
    total_pnl = capped_position_pnl(state, order.is_long, total_pnl)
    base = _trunc_div(total_pnl * order.size_delta_tokens, before.size_tokens)
    if base > 0:
        payout = base // pnl_max
    else:
        payout = 0
    cash = Cash(before.collateral_amount, 0, 0, collateral_min, pnl_min)
    if payout:
        if pnl_token == order.collateral_token:
            cash.output += payout
        else:
            cash.secondary += payout
    if impact > 0:
        amount = impact // pnl_max
        if pnl_token == order.collateral_token:
            cash.output += amount
        else:
            cash.secondary += amount
    payments = []
    insolvent_step = None
    for label, usd in (
        ("funding", funding * collateral_min),
        ("pnl", max(-base, 0)),
        ("fees", other_fees * collateral_min),
        ("impact", max(-impact, 0)),
        ("diff", impact_diff),
    ):
        paid = cash.pay(usd)
        payments.append({"step": label, **paid})
        if paid["remaining_cost_usd"]:
            if not order.is_liquidation:
                raise UnavailableEvidence(f"insolvent at {label} settlement")
            insolvent_step = label
            break
    full_close = order.size_delta_usd == before.size_usd
    if not full_close:
        if order.withdrawal_amount > cash.collateral:
            raise UnavailableEvidence("withdrawal exceeds remaining collateral")
        cash.collateral -= order.withdrawal_amount
        cash.output += order.withdrawal_amount
    else:
        cash.output += cash.collateral
        cash.collateral = 0
    return {
        "base_pnl_usd": base,
        "remaining_collateral": cash.collateral,
        "collateral_output": cash.output,
        "pnl_token_output": cash.secondary,
        "pnl_token": pnl_token,
        "payments": payments,
        "full_close": full_close,
        "insolvent_step": insolvent_step,
    }


def calculate(
    state: EvidenceState,
    order: EconomicOrder,
    before: PositionBefore,
    referral: ReferralTerms | None,
) -> EconomicResult:
    """Calculate one candidate; unsupported or missing evidence is unavailable."""
    try:
        if (min(order.size_delta_usd, order.size_delta_tokens,
                order.execution_fee_wei, order.withdrawal_amount,
                before.size_usd, before.size_tokens, before.collateral_amount) < 0
                or order.size_delta_usd == 0):
            raise UnavailableEvidence("invalid size or cash input")
        if order.is_liquidation and (order.is_increase or
                order.size_delta_usd != before.size_usd or
                order.withdrawal_amount or order.execution_fee_wei):
            raise UnavailableEvidence("liquidation requires full close and zero user execution fee")
        if not order.is_increase and (before.size_usd == 0 or before.size_tokens == 0
                                      or order.size_delta_usd > before.size_usd
                                      or order.size_delta_tokens > before.size_tokens):
            raise UnavailableEvidence("invalid decrease size")
        if not order.is_increase:
            expected_tokens = (
                before.size_tokens if order.size_delta_usd == before.size_usd
                else ceil_div(before.size_tokens * order.size_delta_usd, before.size_usd)
                if order.is_long else before.size_tokens * order.size_delta_usd // before.size_usd
            )
            if order.size_delta_tokens != expected_tokens:
                raise UnavailableEvidence("decrease token amount disagrees with GMX rounding")
        if order.virtual_inventory_tokens is None:
            raise UnavailableEvidence("missing virtual inventory")
        if referral is None:
            raise UnavailableEvidence("missing referral configuration")
        index = state.index_token
        index_min, index_max = state.oracle[index]
        collateral_min, _ = state.oracle[order.collateral_token]
        if min(index_min, collateral_min) <= 0:
            raise UnavailableEvidence("invalid oracle price")
        factors = {name: _config(state, "impact", name) for name in (
            "position_impact_factor_positive", "position_impact_factor_negative",
            "position_impact_exponent_factor_positive", "position_impact_exponent_factor_negative",
            "max_position_impact_factor_positive", "max_position_impact_factor_negative",
            "max_lendable_impact_factor", "max_lendable_impact_usd")}
        signed_tokens = order.size_delta_tokens if order.is_increase else -order.size_delta_tokens
        model = predict_current_impact(
            state.open_interest_tokens["long"], state.open_interest_tokens["short"],
            order.virtual_inventory_tokens, signed_tokens, order.is_long,
            index_min, index_max, factors,
        )
        current = int(model["selected_impact_usd"])
        positive_cap = order.size_delta_usd * factors["max_position_impact_factor_positive"] // FLOAT_PRECISION
        if current > 0:
            current = min(current, positive_cap)
        total, diff = current, 0
        if not order.is_increase:
            pending = _proportional_pending_impact(
                before.pending_impact_amount, order.size_delta_usd, before.size_usd)
            pending_usd = pending * (index_min if pending > 0 else index_max)
            total += pending_usd
            if total < 0:
                cap_factor = (state.configuration.get(
                    "risk:max_position_impact_factor_for_liquidations")
                    if order.is_liquidation else
                    factors["max_position_impact_factor_negative"])
                if type(cap_factor) is not int:
                    raise UnavailableEvidence("missing liquidation impact cap")
                negative_cap = -(order.size_delta_usd * cap_factor // FLOAT_PRECISION)
                if total < negative_cap:
                    diff = negative_cap - total
                    total = negative_cap
            if total > 0:
                if order.is_liquidation:
                    total = 0
                total = min(total, positive_cap)
                if factors["max_lendable_impact_factor"] or factors["max_lendable_impact_usd"]:
                    raise UnavailableEvidence("lendable positive impact cap unsupported")
                total = min(total, state.impact_pool_amount * index_min)
        model = {**model, "current_impact_usd": current,
                 "total_impact_usd": total, "negative_cap_diff_usd": diff}
        oracle_execution = index_max if order.is_increase == order.is_long else index_min
        if order.is_increase:
            impact_amount = _trunc_div(current, index_min if current < 0 else index_max)
            effective_tokens = order.size_delta_tokens + (impact_amount if order.is_long else -impact_amount)
            if effective_tokens <= 0:
                raise UnavailableEvidence("nonpositive impact-adjusted token amount")
            execution_price = order.size_delta_usd // effective_tokens
        else:
            adjustment = _trunc_div(
                _trunc_div(before.size_usd * (current if order.is_long else -current),
                           before.size_tokens), order.size_delta_usd)
            execution_price = oracle_execution + adjustment
        if execution_price <= 0:
            raise UnavailableEvidence("nonpositive execution price")
        acceptable = order.acceptable_price
        buy = order.is_increase == order.is_long
        acceptable_met = (True if acceptable is None else
                          execution_price <= acceptable if buy else execution_price >= acceptable)
        if acceptable is not None and acceptable <= 0:
            raise UnavailableEvidence("invalid acceptable price")
        improved = model["balance_was_improved"]
        factor = _config(state, "fee", "position_fee_positive" if improved else "position_fee_negative")
        position_fee = order.size_delta_usd * factor // FLOAT_PRECISION // collateral_min
        referral_amounts = discount_amounts(position_fee, referral.code,
                                            referral.rebate_bps, referral.share_bps,
                                            referral.minimum, referral.pro_tier,
                                            referral.pro_factor)
        discount = referral_amounts["totalDiscountAmount"]
        market = state.market
        if not market:
            raise UnavailableEvidence("missing market identity")
        current_borrow = _storage(state, "CUMULATIVE_BORROWING_FACTOR", market, order.is_long)
        borrowing_delta = current_borrow - before.borrowing_factor
        funding_latest = _storage(state, "FUNDING_FEE_AMOUNT_PER_SIZE",
                                  market, order.collateral_token, order.is_long)
        funding_delta = funding_latest - before.funding_fee_per_size
        if min(borrowing_delta, funding_delta) < 0:
            raise UnavailableEvidence("negative accrual interval")
        borrowing_usd = before.size_usd * borrowing_delta // FLOAT_PRECISION
        borrowing_fee = borrowing_usd // collateral_min
        funding_fee = ceil_div(before.size_usd * funding_delta, FUNDING_PRECISION)
        ui_receiver = order.ui_fee_receiver.lower()
        ui_factor = (0 if ui_receiver == ZERO else
                     min(_config(state, "fee", "ui_fee:" + ui_receiver),
                         _config(state, "fee", "max_ui_fee")))
        ui_fee = order.size_delta_usd * ui_factor // FLOAT_PRECISION // collateral_min
        liquidation = 0
        if order.is_liquidation:
            liquidation_factor = _config(state, "risk", "liquidation_fee_factor")
            liquidation = ceil_div(order.size_delta_usd * liquidation_factor // FLOAT_PRECISION,
                                   collateral_min)
        total_fee = position_fee + borrowing_fee + funding_fee + ui_fee + liquidation - discount
        if total_fee < 0:
            raise UnavailableEvidence("negative net position charge")
        fees = {"position_fee_amount": position_fee, "borrowing_fee_usd": borrowing_usd,
                "borrowing_fee_amount": borrowing_fee, "funding_fee_amount": funding_fee,
                "ui_fee_amount": ui_fee, "liquidation_fee_amount": liquidation,
                "discount_amount": discount, "total_cost_amount": total_fee,
                "protocol_fee_amount": referral_amounts["protocolFeeAmount"],
                "affiliate_reward_amount": referral_amounts["referral.affiliateRewardAmount"]}
        settlement = None
        if not order.is_increase and acceptable_met:
            settlement = _settle_decrease(state, order, before, total, diff,
                                          funding_fee, total_fee - funding_fee)
        return EconomicResult("eligible" if acceptable_met else "acceptable_price_rejected",
                              None, model, fees, execution_price, acceptable_met,
                              settlement, order.execution_fee_wei,
                              calculation_digest(state, order, before))
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as error:
        return EconomicResult("unavailable", str(error), {}, {}, None, None, None,
                              order.execution_fee_wei)
