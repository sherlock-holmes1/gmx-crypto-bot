"""Conservative replay risk points for a simulated GMX position.

Historical risk thresholds are explicit inputs. No observed liquidation event or
fee output is used to decide whether the hypothetical position is at risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.models.accrual import ceil_div
from gmx_crypto_bot_v2.models.impact import predict_current_impact
from gmx_crypto_bot_v2.models.referral import discount_amounts
from gmx_crypto_bot_v2.simulation.economics import ReferralTerms
from gmx_crypto_bot_v2.simulation.evidence import EvidenceState, UnavailableEvidence
from gmx_crypto_bot_v2.simulation.ledger import PositionLedger
from gmx_crypto_bot_v2.simulation.pnl_cap import capped_position_pnl

P = 10**30
Coordinate = tuple[int, int, int]


@dataclass(frozen=True)
class RiskConfiguration:
    coordinate: Coordinate
    valid_through_block: int
    min_collateral_usd: int
    min_collateral_factor: int
    max_leverage_factor: int
    source: str
    liquidation_negative_impact_factor: int | None = None
    ui_fee_factor: int | None = None

    def __post_init__(self) -> None:
        if (self.valid_through_block < self.coordinate[0]
                or min(self.min_collateral_usd, self.min_collateral_factor,
                       self.max_leverage_factor) < 0
                or self.min_collateral_factor > P
                or (self.liquidation_negative_impact_factor is not None
                    and self.liquidation_negative_impact_factor < 0)
                or (self.ui_fee_factor is not None
                    and not 0 <= self.ui_fee_factor <= P)
                or not self.source):
            raise ValueError("invalid risk configuration")


@dataclass(frozen=True)
class RiskReferralEvidence:
    """Account-specific referral/pro terms pinned over this replay interval."""

    coordinate: Coordinate
    valid_through_block: int
    terms: ReferralTerms
    source: str

    def __post_init__(self) -> None:
        if self.valid_through_block < self.coordinate[0] or not self.source:
            raise ValueError("invalid referral evidence")


@dataclass(frozen=True)
class RiskPoint:
    coordinate: Coordinate
    status: str
    reason: str | None
    adverse_index_price: int | None = None
    collateral_price: int | None = None
    pnl_usd: int | None = None
    borrowing_usd: int | None = None
    funding_usd: int | None = None
    pending_impact_usd: int | None = None
    full_close_impact_usd: int | None = None
    close_fee_usd: int | None = None
    close_discount_amount: int | None = None
    remaining_collateral_usd: int | None = None
    requirement_usd: int | None = None
    buffer_usd: int | None = None
    leverage_factor: int | None = None
    liquidatable: bool | None = None
    liquidation_settlement: str = "unavailable"


@dataclass(frozen=True)
class RiskPath:
    status: str
    points: tuple[RiskPoint, ...]
    first_liquidation: Coordinate | None
    stop_execution: Coordinate | None
    reason: str | None = None


def _fail(state: EvidenceState, reason: str) -> RiskPoint:
    return RiskPoint(state.coordinate, "unavailable", reason)


def assess(
    state: EvidenceState,
    position: PositionLedger,
    configuration: RiskConfiguration | None,
    *,
    virtual_inventory_tokens: int | None = None,
    referral: RiskReferralEvidence | None = None,
) -> RiskPoint:
    """Value a position at one recorded state using adverse oracle bounds."""
    try:
        if position.size_usd == 0:
            return RiskPoint(state.coordinate, "closed", None, liquidatable=False)
        if configuration is None:
            raise UnavailableEvidence("missing historical risk configuration")
        if (referral is None or not referral.coordinate <= state.coordinate
                or state.coordinate[0] > referral.valid_through_block):
            raise UnavailableEvidence("missing account-specific referral coverage")
        if (configuration.liquidation_negative_impact_factor is None
                or type(virtual_inventory_tokens) is not int):
            raise UnavailableEvidence("missing full-close impact or fee evidence")
        if not (configuration.coordinate <= state.coordinate
                and state.coordinate[0] <= configuration.valid_through_block):
            raise UnavailableEvidence("risk configuration does not cover coordinate")
        if state.counterfactual_applied is False:
            raise UnavailableEvidence("risk requires counterfactual market overlay")
        index_prices = state.oracle.get(state.index_token)
        collateral_prices = state.oracle.get(position.collateral_token)
        if not index_prices or not collateral_prices:
            raise UnavailableEvidence("missing adverse oracle range")
        index = index_prices[0] if position.is_long else index_prices[1]
        collateral = collateral_prices[0]
        if min(index, collateral) <= 0:
            raise UnavailableEvidence("invalid adverse oracle price")
        if min(position.size_usd, position.size_tokens, position.collateral_usdc) < 0:
            raise UnavailableEvidence("negative position state")
        pnl = position.size_tokens * index - position.size_usd
        if not position.is_long:
            pnl = -pnl
        pnl = capped_position_pnl(state, position.is_long, pnl)
        market = state.market
        borrowing_now = state.accrual.get(key("CUMULATIVE_BORROWING_FACTOR",
                                              market, position.is_long))
        funding_now = state.accrual.get(key("FUNDING_FEE_AMOUNT_PER_SIZE", market,
                                            position.collateral_token, position.is_long))
        if type(borrowing_now) is not int or type(funding_now) is not int:
            raise UnavailableEvidence("missing risk accrual accumulator")
        borrowing_delta = borrowing_now - position.borrowing_factor
        funding_delta = funding_now - position.funding_fee_per_size
        if min(borrowing_delta, funding_delta) < 0:
            raise UnavailableEvidence("negative risk accrual interval")
        borrowing_gross = position.size_usd * borrowing_delta // P
        borrowing = (borrowing_gross // collateral) * collateral
        funding_tokens = ceil_div(position.size_usd * funding_delta, 10**45)
        funding = funding_tokens * collateral
        factors = {}
        for name in (
            "position_impact_factor_positive", "position_impact_factor_negative",
            "position_impact_exponent_factor_positive",
            "position_impact_exponent_factor_negative",
        ):
            value = state.configuration.get("impact:" + name)
            if type(value) is not int or value < 0:
                raise UnavailableEvidence("missing historical full-close impact factor")
            factors[name] = value
        impact_model = predict_current_impact(
            state.open_interest_tokens["long"],
            state.open_interest_tokens["short"],
            virtual_inventory_tokens,
            -position.size_tokens,
            position.is_long,
            index_prices[0], index_prices[1], factors,
        )
        pending = position.pending_impact_amount * (
            index_prices[0] if position.pending_impact_amount >= 0 else index_prices[1])
        # Liquidation uses a zero positive cap and its separate negative cap.
        # The current full-close impact and proportional pending amount are
        # combined before that cap is applied.
        full_close_impact = min(0, int(impact_model["selected_impact_usd"]) + pending)
        full_close_impact = max(
            full_close_impact,
            -(position.size_usd * configuration.liquidation_negative_impact_factor // P),
        )
        fee_name = ("position_fee_positive" if impact_model["balance_was_improved"]
                    else "position_fee_negative")
        position_fee_factor = state.configuration.get("fee:" + fee_name)
        if type(position_fee_factor) is not int or position_fee_factor < 0:
            raise UnavailableEvidence("missing full-close position fee factor")
        position_fee_tokens = (position.size_usd * position_fee_factor // P // collateral)
        terms = referral.terms
        discount = discount_amounts(
            position_fee_tokens, terms.code, terms.rebate_bps, terms.share_bps,
            terms.minimum, terms.pro_tier, terms.pro_factor,
        )["totalDiscountAmount"]
        # GMX's isPositionLiquidatable fee estimate uses a zero UI receiver.
        close_fee = (position_fee_tokens - discount) * collateral
        remaining = (position.collateral_usdc * collateral + pnl
                     - borrowing - funding + full_close_impact - close_fee)
        threshold = max(configuration.min_collateral_usd,
                        position.size_usd * configuration.min_collateral_factor // P)
        if configuration.max_leverage_factor:
            threshold = max(threshold,
                            ceil_div(position.size_usd * P,
                                     configuration.max_leverage_factor))
        buffer = remaining - threshold
        leverage = position.size_usd * P // remaining if remaining > 0 else None
        return RiskPoint(
            coordinate=state.coordinate, status="available", reason=None,
            adverse_index_price=index, collateral_price=collateral, pnl_usd=pnl,
            borrowing_usd=borrowing, funding_usd=funding,
            pending_impact_usd=pending, full_close_impact_usd=full_close_impact,
            close_fee_usd=close_fee, close_discount_amount=discount,
            remaining_collateral_usd=remaining,
            requirement_usd=threshold, buffer_usd=buffer,
            leverage_factor=leverage, liquidatable=remaining <= 0 or buffer < 0,
            # Settling liquidation requires independently modeled full-close
            # caps, market effects and insolvency, which are not yet covered.
            liquidation_settlement="unavailable",
        )
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as error:
        return _fail(state, str(error))


def monitor_path(
    states: Iterable[EvidenceState],
    position: PositionLedger,
    configurations: Mapping[Coordinate, RiskConfiguration],
    expected_coordinates: Iterable[Coordinate],
    *,
    virtual_inventory: Mapping[Coordinate, int] | None = None,
    referrals: Mapping[Coordinate, RiskReferralEvidence] | None = None,
    stop_execution: Coordinate | None = None,
) -> RiskPath:
    """Require every declared replay state; pending stops do not suppress risk."""
    virtual_inventory = virtual_inventory or {}
    referrals = referrals or {}
    points = tuple(sorted((assess(state, position, configurations.get(state.coordinate),
                                  virtual_inventory_tokens=virtual_inventory.get(state.coordinate),
                                  referral=referrals.get(state.coordinate))
                           for state in states), key=lambda point: point.coordinate))
    expected = tuple(sorted(expected_coordinates))
    actual = tuple(point.coordinate for point in points)
    if not expected or actual != expected or len(set(actual)) != len(actual):
        return RiskPath("unavailable", points, None, stop_execution,
                        "missing or duplicate risk coverage coordinate")
    first = next((point.coordinate for point in points
                  if point.liquidatable is True
                  and (stop_execution is None or point.coordinate <= stop_execution)),
                 None)
    if any(point.status == "unavailable" for point in points):
        return RiskPath("unavailable", points, first, stop_execution,
                        "risk evidence unavailable within replay interval")
    if first is not None:
        return RiskPath("liquidation_before_stop", points, first, stop_execution)
    return RiskPath("covered", points, None, stop_execution)
