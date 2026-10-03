"""Pure scheduling of hypothetical GMX requests against later keeper opportunities.

This module establishes timing and trigger eligibility only. It does not claim
that a hypothetical request was accepted on chain or calculate its fees.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderKind(str, Enum):
    MARKET_INCREASE = "market_increase"
    MARKET_DECREASE = "market_decrease"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class Request:
    kind: OrderKind
    is_long: bool
    created_block: int
    trigger_price: int | None = None
    acceptable_price: int | None = None

    def __post_init__(self) -> None:
        if self.created_block < 0:
            raise ValueError("negative request block")
        if self.kind in {OrderKind.STOP_LOSS, OrderKind.TAKE_PROFIT}:
            if self.trigger_price is None or self.trigger_price <= 0:
                raise ValueError("conditional request needs a positive trigger price")
        elif self.trigger_price is not None:
            raise ValueError("market request cannot have a trigger price")
        if self.acceptable_price is not None and self.acceptable_price <= 0:
            raise ValueError("acceptable price must be positive")

    @property
    def is_increase(self) -> bool:
        return self.kind is OrderKind.MARKET_INCREASE


@dataclass(frozen=True)
class KeeperOpportunity:
    block: int
    transaction_index: int
    log_index: int
    oracle_min: int
    oracle_max: int
    trigger_price: int | None = None

    def __post_init__(self) -> None:
        if min(self.block, self.transaction_index, self.log_index) < 0:
            raise ValueError("negative keeper coordinate")
        if self.oracle_min <= 0 or self.oracle_max < self.oracle_min:
            raise ValueError("invalid oracle range")
        if self.trigger_price is not None and self.trigger_price <= 0:
            raise ValueError("invalid trigger observation")


@dataclass(frozen=True)
class Attempt:
    opportunity: KeeperOpportunity
    oracle_price: int
    trigger_met: bool
    acceptable_met: bool | None


@dataclass(frozen=True)
class ScheduleResult:
    status: str
    attempt: Attempt | None
    cancellation_block: int | None = None


def _oracle_side(request: Request, opportunity: KeeperOpportunity) -> int:
    """Apply the unfavorable bound for position execution."""
    use_max = request.is_increase == request.is_long
    return opportunity.oracle_max if use_max else opportunity.oracle_min


def _trigger_met(request: Request, price: int) -> bool:
    trigger = request.trigger_price
    if trigger is None:
        return True
    if request.kind is OrderKind.STOP_LOSS:
        return price <= trigger if request.is_long else price >= trigger
    return price >= trigger if request.is_long else price <= trigger


def acceptable_price_met(request: Request, execution_price: int) -> bool:
    acceptable = request.acceptable_price
    if acceptable is None:
        return True
    buy = request.is_increase == request.is_long
    return execution_price <= acceptable if buy else execution_price >= acceptable


def schedule(
    request: Request,
    opportunities: list[KeeperOpportunity],
    *,
    inclusion_delay_blocks: int,
    keeper_delay_blocks: int,
    cancellation_requested_block: int | None = None,
    cancellation_delay_blocks: int = 0,
) -> ScheduleResult:
    """Choose the first eligible later opportunity under a block-delay scenario.

    The opportunity is only a candidate. The economic model must calculate the
    impact-adjusted execution price and apply the acceptable-price check.
    A trigger observation must be supplied for conditional requests; oracle
    min/max alone do not establish trigger eligibility. Cancellation takes
    effect at its modeled inclusion block.
    """
    if min(inclusion_delay_blocks, keeper_delay_blocks, cancellation_delay_blocks) < 0:
        raise ValueError("negative delay")
    if (
        cancellation_requested_block is not None
        and cancellation_requested_block < request.created_block
    ):
        raise ValueError("cancellation precedes request")
    earliest = request.created_block + inclusion_delay_blocks + keeper_delay_blocks
    cancellation_block = (
        None if cancellation_requested_block is None
        else cancellation_requested_block + cancellation_delay_blocks
    )
    for opportunity in sorted(
        opportunities,
        key=lambda item: (item.block, item.transaction_index, item.log_index),
    ):
        if opportunity.block <= request.created_block or opportunity.block < earliest:
            continue
        if cancellation_block is not None and opportunity.block >= cancellation_block:
            return ScheduleResult("cancelled", None, cancellation_block)
        oracle_price = _oracle_side(request, opportunity)
        if request.trigger_price is not None and opportunity.trigger_price is None:
            return ScheduleResult("unavailable", None, cancellation_block)
        observed_trigger = opportunity.trigger_price
        trigger_met = _trigger_met(
            request, observed_trigger if observed_trigger is not None else oracle_price
        )
        if not trigger_met:
            continue
        attempt = Attempt(opportunity, oracle_price, True, None)
        return ScheduleResult("candidate", attempt, cancellation_block)
    latest_observed = max((item.block for item in opportunities), default=None)
    if cancellation_block is not None and (
        latest_observed is not None and cancellation_block <= latest_observed
    ):
        return ScheduleResult("cancelled", None, cancellation_block)
    return ScheduleResult("unresolved", None, cancellation_block)
