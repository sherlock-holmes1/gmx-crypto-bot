"""Counterfactual position and market overlays, separate from observed traders."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.models.arithmetic import _proportional_pending_impact, _trunc_div
from gmx_crypto_bot_v2.simulation.economics import (
    EconomicOrder, EconomicResult, PositionBefore, calculation_digest,
)
from gmx_crypto_bot_v2.simulation.evidence import EvidenceState, UnavailableEvidence


@dataclass(frozen=True)
class PositionLedger:
    is_long: bool
    collateral_token: str
    cash_usdc: int
    cash_eth_wei: int
    size_usd: int = 0
    size_tokens: int = 0
    collateral_usdc: int = 0
    pending_impact_amount: int = 0
    borrowing_factor: int = 0
    funding_fee_per_size: int = 0
    released_usdc: int = 0
    released_pnl_tokens: int = 0
    realized_pnl_usd: int = 0
    position_charges_usdc: int = 0
    execution_fees_wei: int = 0

    def before(self) -> PositionBefore:
        return PositionBefore(
            self.size_usd, self.size_tokens, self.collateral_usdc,
            self.borrowing_factor, self.funding_fee_per_size,
            self.pending_impact_amount,
        )


@dataclass(frozen=True)
class PoolEffect:
    """Independently modeled per-token pool and impact-pool deltas.

    A transition requires this explicit evidence. An unknown effect is never
    silently represented by an empty mapping or zero impact amount.
    """

    pool_amount: Mapping[str, int]
    impact_pool_tokens: int
    source: str


def fee_only_increase_pool_effect(
    state: EvidenceState, order: EconomicOrder, economics: EconomicResult
) -> PoolEffect:
    """Derive the fee deposit for a simple increase from independent charges.

    Pending position impact stays in the position and does not itself credit
    the position-impact pool. Decreases need PnL/pool accounting and must use
    a separately derived effect.
    """
    if not order.is_increase or economics.status != "eligible":
        raise UnavailableEvidence("fee-only pool effect needs eligible increase")
    charges = economics.fees
    if charges.get("discount_amount") != 0:
        raise UnavailableEvidence("referral pool distribution unavailable")
    position = charges.get("position_fee_amount")
    borrowing = charges.get("borrowing_fee_amount")
    position_receiver = state.configuration.get("fee:position_fee_receiver")
    borrowing_receiver = state.configuration.get("fee:borrowing_fee_receiver")
    values = (position, borrowing, position_receiver, borrowing_receiver)
    if any(type(value) is not int or value < 0 for value in values):
        raise UnavailableEvidence("fee distribution configuration unavailable")
    precision = 10**30
    if position_receiver > precision or borrowing_receiver > precision:
        raise UnavailableEvidence("invalid fee receiver factor")
    deposited = (position - position * position_receiver // precision
                 + borrowing - borrowing * borrowing_receiver // precision)
    return PoolEffect({order.collateral_token: deposited}, 0,
                      "independent_fee_distribution")


def fee_only_decrease_pool_effect(
    state: EvidenceState, order: EconomicOrder, economics: EconomicResult
) -> PoolEffect:
    """Derive pool fees for a flat decrease with no PnL or impact transfer."""
    if order.is_increase or economics.status != "eligible" or economics.settlement is None:
        raise UnavailableEvidence("fee-only decrease effect needs settled decrease")
    settlement = economics.settlement
    if (settlement.get("base_pnl_usd") != 0
            or settlement.get("pnl_token_output") != 0
            or economics.impact.get("total_impact_usd") != 0
            or economics.impact.get("negative_cap_diff_usd") != 0
            or economics.fees.get("funding_fee_amount") != 0):
        raise UnavailableEvidence("decrease pool PnL, impact or funding effect unavailable")
    equivalent_increase = replace(order, is_increase=True)
    effect = fee_only_increase_pool_effect(state, equivalent_increase, economics)
    return replace(effect, source="independent_flat_decrease_fee_distribution")


@dataclass(frozen=True)
class CounterfactualBook:
    long_oi_usd: int = 0
    short_oi_usd: int = 0
    long_oi_tokens: int = 0
    short_oi_tokens: int = 0
    pool_amount: Mapping[str, int] = field(default_factory=dict)
    impact_pool_tokens: int = 0
    oi_usd_cells: Mapping[tuple[str, bool], int] = field(default_factory=dict)
    oi_token_cells: Mapping[tuple[str, bool], int] = field(default_factory=dict)

    def apply_to(self, observed: EvidenceState) -> EvidenceState:
        """Overlay this simulator's deltas on a fresh observed pre-event state."""
        if observed.counterfactual_applied:
            raise UnavailableEvidence("counterfactual state cannot be overlaid twice")
        if (sum(v for (_, side), v in self.oi_usd_cells.items() if side)
                != self.long_oi_usd
                or sum(v for (_, side), v in self.oi_usd_cells.items() if not side)
                != self.short_oi_usd
                or sum(v for (_, side), v in self.oi_token_cells.items() if side)
                != self.long_oi_tokens
                or sum(v for (_, side), v in self.oi_token_cells.items() if not side)
                != self.short_oi_tokens):
            raise UnavailableEvidence("counterfactual OI cells do not balance totals")
        usd = dict(observed.open_interest_usd)
        tokens = dict(observed.open_interest_tokens)
        pools = dict(observed.pool_amount)
        usd["long"] += self.long_oi_usd
        usd["short"] += self.short_oi_usd
        tokens["long"] += self.long_oi_tokens
        tokens["short"] += self.short_oi_tokens
        for token, delta in self.pool_amount.items():
            if token not in pools:
                raise UnavailableEvidence("counterfactual pool token missing from market")
            pools[token] += delta
        impact = observed.impact_pool_amount + self.impact_pool_tokens
        if min(*usd.values(), *tokens.values(), *pools.values(), impact) < 0:
            raise UnavailableEvidence("counterfactual market state below zero")
        # The storage map is also overlaid so later accrual formulas see the
        # simulated OI/pool, while the original recording remains untouched.
        storage = dict(observed.accrual)
        for name, cells in (
            ("OPEN_INTEREST", self.oi_usd_cells),
            ("OPEN_INTEREST_IN_TOKENS", self.oi_token_cells),
        ):
            for (token, side), delta in cells.items():
                slot = key(name, observed.market, token, side)
                if delta:
                    if slot not in storage:
                        raise UnavailableEvidence("counterfactual OI storage cell missing")
                    storage[slot] += delta
        for token, delta in self.pool_amount.items():
            slot = key("POOL_AMOUNT", observed.market, token)
            if delta:
                if slot not in storage:
                    raise UnavailableEvidence("counterfactual pool storage cell missing")
                storage[slot] += delta
        return replace(observed, open_interest_usd=usd, open_interest_tokens=tokens,
                       pool_amount=pools, impact_pool_amount=impact, accrual=storage,
                       counterfactual_applied=True)


@dataclass(frozen=True)
class LedgerTransition:
    ledger: PositionLedger
    market: CounterfactualBook
    position_charge_usdc: int
    collateral_deposit_usdc: int
    collateral_release_usdc: int
    execution_fee_wei: int


def apply_fill(
    ledger: PositionLedger,
    market: CounterfactualBook,
    state: EvidenceState,
    order: EconomicOrder,
    economics: EconomicResult,
    pool_effect: PoolEffect | None,
) -> LedgerTransition:
    """Commit one eligible simulated fill after all evidence checks pass."""
    if economics.status != "eligible" or economics.acceptable_met is not True:
        raise UnavailableEvidence("candidate has no eligible economic fill")
    if state.counterfactual_applied:
        raise UnavailableEvidence("fill needs fresh observed market state")
    expected_state = market.apply_to(state)
    if economics.calculation_digest != calculation_digest(
        expected_state, order, ledger.before()
    ):
        raise UnavailableEvidence("economics was not calculated from current market overlay")
    if pool_effect is None or not pool_effect.source or not pool_effect.pool_amount:
        raise UnavailableEvidence("independent pool effect unavailable")
    if order.is_long != ledger.is_long or order.collateral_token != ledger.collateral_token:
        raise UnavailableEvidence("order does not match position ledger")
    if order.is_liquidation:
        raise UnavailableEvidence("liquidation ledger unavailable")
    if min(order.collateral_delta_amount, economics.execution_fee_wei) < 0:
        raise UnavailableEvidence("negative cash movement")
    if economics.execution_fee_wei != order.execution_fee_wei:
        raise UnavailableEvidence("execution fee budget mismatch")
    if ledger.cash_eth_wei < economics.execution_fee_wei:
        raise UnavailableEvidence("insufficient ETH execution-fee cash")
    current_borrow = state.accrual.get(key("CUMULATIVE_BORROWING_FACTOR",
                                           state.market, ledger.is_long))
    current_funding = state.accrual.get(key("FUNDING_FEE_AMOUNT_PER_SIZE",
                                            state.market, ledger.collateral_token,
                                            ledger.is_long))
    if type(current_borrow) is not int or type(current_funding) is not int:
        raise UnavailableEvidence("latest accrual accumulator unavailable")
    charges = economics.fees.get("total_cost_amount")
    if type(charges) is not int or charges < 0:
        raise UnavailableEvidence("position charges unavailable")
    if order.is_increase:
        if economics.settlement is not None:
            raise UnavailableEvidence("increase cannot have decrease settlement")
        deposit = order.collateral_delta_amount
        if deposit > ledger.cash_usdc:
            raise UnavailableEvidence("insufficient USDC deposit cash")
        collateral = ledger.collateral_usdc + deposit - charges
        if collateral < 0:
            raise UnavailableEvidence("increase charges exceed collateral")
        impact_usd = economics.impact.get("current_impact_usd")
        price = state.oracle.get(state.index_token)
        if type(impact_usd) is not int or price is None:
            raise UnavailableEvidence("pending impact amount unavailable")
        impact_amount = _trunc_div(impact_usd, price[0] if impact_usd < 0 else price[1])
        next_ledger = replace(
            ledger, cash_usdc=ledger.cash_usdc - deposit,
            cash_eth_wei=ledger.cash_eth_wei - economics.execution_fee_wei,
            size_usd=ledger.size_usd + order.size_delta_usd,
            size_tokens=ledger.size_tokens + order.size_delta_tokens,
            collateral_usdc=collateral,
            pending_impact_amount=ledger.pending_impact_amount + impact_amount,
            borrowing_factor=current_borrow,
            funding_fee_per_size=current_funding,
            position_charges_usdc=ledger.position_charges_usdc + charges,
            execution_fees_wei=ledger.execution_fees_wei + economics.execution_fee_wei,
        )
        release = 0
    else:
        settlement = economics.settlement
        if settlement is None:
            raise UnavailableEvidence("decrease settlement unavailable")
        deposit = 0
        release = settlement["collateral_output"]
        remaining_usd = ledger.size_usd - order.size_delta_usd
        remaining_tokens = ledger.size_tokens - order.size_delta_tokens
        if min(release, remaining_usd, remaining_tokens,
               settlement["remaining_collateral"], settlement["pnl_token_output"]) < 0:
            raise UnavailableEvidence("negative decrease ledger amount")
        if (settlement["full_close"] != (remaining_usd == 0)
                or (remaining_usd == 0 and remaining_tokens != 0)):
            raise UnavailableEvidence("inconsistent full-close settlement")
        pending_released = _proportional_pending_impact(
            ledger.pending_impact_amount, order.size_delta_usd, ledger.size_usd)
        collateral_min = state.oracle[ledger.collateral_token][0]
        realized = (settlement["base_pnl_usd"]
                    + economics.impact["total_impact_usd"]
                    - charges * collateral_min)
        next_ledger = replace(
            ledger, cash_usdc=ledger.cash_usdc + release,
            cash_eth_wei=ledger.cash_eth_wei - economics.execution_fee_wei,
            size_usd=remaining_usd, size_tokens=remaining_tokens,
            collateral_usdc=settlement["remaining_collateral"],
            pending_impact_amount=ledger.pending_impact_amount - pending_released,
            borrowing_factor=current_borrow,
            funding_fee_per_size=current_funding,
            released_usdc=ledger.released_usdc + release,
            released_pnl_tokens=ledger.released_pnl_tokens + settlement["pnl_token_output"],
            realized_pnl_usd=ledger.realized_pnl_usd + realized,
            position_charges_usdc=ledger.position_charges_usdc + charges,
            execution_fees_wei=ledger.execution_fees_wei + economics.execution_fee_wei,
        )
    side = "long" if order.is_long else "short"
    signed_usd = order.size_delta_usd if order.is_increase else -order.size_delta_usd
    signed_tokens = order.size_delta_tokens if order.is_increase else -order.size_delta_tokens
    pools = dict(market.pool_amount)
    for token, delta in pool_effect.pool_amount.items():
        if token not in state.pool_amount or type(delta) is not int:
            raise UnavailableEvidence("unsupported pool effect token or amount")
        pools[token] = pools.get(token, 0) + delta
    usd_cells = dict(market.oi_usd_cells)
    token_cells = dict(market.oi_token_cells)
    cell = (order.collateral_token, order.is_long)
    usd_cells[cell] = usd_cells.get(cell, 0) + signed_usd
    token_cells[cell] = token_cells.get(cell, 0) + signed_tokens
    next_market = replace(
        market, long_oi_usd=market.long_oi_usd + (signed_usd if side == "long" else 0),
        short_oi_usd=market.short_oi_usd + (signed_usd if side == "short" else 0),
        long_oi_tokens=market.long_oi_tokens + (signed_tokens if side == "long" else 0),
        short_oi_tokens=market.short_oi_tokens + (signed_tokens if side == "short" else 0),
        pool_amount=pools,
        impact_pool_tokens=market.impact_pool_tokens + pool_effect.impact_pool_tokens,
        oi_usd_cells=usd_cells, oi_token_cells=token_cells,
    )
    next_market.apply_to(state)
    return LedgerTransition(next_ledger, next_market, charges, deposit, release,
                            economics.execution_fee_wei)
