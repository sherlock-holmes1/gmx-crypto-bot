"""Predeclared GMX simulator scenarios and evidence-linked result reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from itertools import product
from typing import Callable, Mapping, Protocol

from gmx_crypto_bot_v2.simulation.economics import (
    EconomicOrder, EconomicResult, ReferralTerms, calculate,
)
from gmx_crypto_bot_v2.simulation.evidence import EvidenceState, UnavailableEvidence
from gmx_crypto_bot_v2.simulation.ledger import (
    CounterfactualBook, PoolEffect, PositionLedger, apply_fill,
    fee_only_decrease_pool_effect, fee_only_increase_pool_effect,
)
from gmx_crypto_bot_v2.simulation.orders import (
    KeeperOpportunity, Request, schedule,
)
from gmx_crypto_bot_v2.simulation.risk import (
    RiskConfiguration, RiskPoint, RiskReferralEvidence, assess,
)

Coordinate = tuple[int, int, int]


class ScenarioEvidence(Protocol):
    def keeper_opportunities(self) -> list[KeeperOpportunity]: ...
    def state_for_opportunity(self, opportunity: KeeperOpportunity) -> EvidenceState: ...
    def at(self, coordinate: Coordinate) -> EvidenceState: ...
    def required_risk_coordinates(self) -> tuple[Coordinate, ...]: ...


@dataclass(frozen=True)
class PlannedOrder:
    request_coordinate: Coordinate
    scheduling: Request
    economics: EconomicOrder
    referral: ReferralTerms | None
    pool_effect: PoolEffect | None = None
    cancellation_requested_block: int | None = None
    cancellation_delay_blocks: int = 0

    def __post_init__(self) -> None:
        if self.request_coordinate[0] != self.scheduling.created_block:
            raise ValueError("request coordinate disagrees with scheduler block")


@dataclass(frozen=True)
class Scenario:
    name: str
    inclusion_delay_blocks: int
    keeper_delay_blocks: int
    execution_fee_wei: int
    acceptable_price_override: int | None
    strategy_parameters: Mapping[str, int | str | bool]
    liquidation_buffer_usd: int

    def __post_init__(self) -> None:
        if (not self.name or min(self.inclusion_delay_blocks,
                self.keeper_delay_blocks, self.execution_fee_wei,
                self.liquidation_buffer_usd) < 0):
            raise ValueError("invalid predeclared scenario")


@dataclass(frozen=True)
class OrderRecord:
    request_coordinate: Coordinate
    keeper_coordinate: Coordinate | None
    state_coordinate: Coordinate | None
    status: str
    reason: str | None
    historical_configuration: Mapping[str, int] | None
    impact: Mapping[str, int | bool]
    cost_breakdown: Mapping[str, int]
    execution_price: int | None
    usdc_deposit: int
    usdc_release: int
    eth_execution_fee_wei: int
    ledger_after: Mapping[str, object] | None
    historical_accrual: Mapping[str, int] | None = None
    collateral_price: int | None = None


@dataclass(frozen=True)
class RiskRecord:
    point: RiskPoint
    cash_usdc: int
    size_usd: int
    coordinate_timestamp: int | None
    collateral_price: int | None
    risk_configuration: RiskConfiguration | None = None
    referral_source: str | None = None
    cash_eth_wei: int = 0
    collateral_usdc: int = 0
    ledger_snapshot: Mapping[str, object] = field(default_factory=dict)
    eth_price: int | None = None


@dataclass(frozen=True)
class ScenarioReport:
    scenario: Scenario
    status: str
    orders: tuple[OrderRecord, ...]
    risk: tuple[RiskRecord, ...]
    metrics: Mapping[str, int | None]
    unavailable_reasons: tuple[str, ...]


def scenario_grid(
    *,
    inclusion_delays: tuple[int, ...],
    keeper_delays: tuple[int, ...],
    execution_fees_wei: tuple[int, ...],
    acceptable_prices: tuple[int | None, ...],
    strategy_parameters: tuple[Mapping[str, int | str | bool], ...],
    liquidation_buffer_usd: int,
) -> tuple[Scenario, ...]:
    """Freeze the full grid before any recording evidence is inspected."""
    if not all((inclusion_delays, keeper_delays, execution_fees_wei,
                acceptable_prices, strategy_parameters)):
        raise ValueError("scenario grid axis is empty")
    result = []
    for index, (inclusion, keeper, fee, acceptable, parameters) in enumerate(product(
        inclusion_delays, keeper_delays, execution_fees_wei,
        acceptable_prices, strategy_parameters
    )):
        result.append(Scenario(f"scenario-{index + 1}", inclusion, keeper, fee,
                               acceptable, dict(parameters), liquidation_buffer_usd))
    return tuple(result)


def _empty_record(plan: PlannedOrder, status: str, reason: str | None,
                  opportunity: KeeperOpportunity | None = None,
                  state: EvidenceState | None = None,
                  economics: EconomicResult | None = None) -> OrderRecord:
    candidate = (None if opportunity is None else
                 (opportunity.block, opportunity.transaction_index,
                  opportunity.log_index))
    return OrderRecord(
        plan.request_coordinate, candidate,
        None if state is None else state.coordinate,
        status, reason, None if state is None else dict(state.configuration),
        {} if economics is None else economics.impact,
        {} if economics is None else economics.fees,
        None if economics is None else economics.execution_price,
        0, 0, 0, None,
    )


def _metrics(
    scenario: Scenario,
    orders: list[OrderRecord],
    risk: list[RiskRecord],
    initial: PositionLedger,
    final: PositionLedger,
    complete: bool,
) -> Mapping[str, int | None]:
    fields = (
        "net_pnl_usd", "collateral_return_factor", "peak_notional_usd",
        "peak_leverage_factor", "max_drawdown_usd", "funding_amount_usdc",
        "borrowing_fee_usd", "impact_usd", "position_fees_usdc",
        "total_position_cost_usdc", "execution_fees_wei", "liquidation_count",
        "seconds_in_liquidation_buffer",
    )
    if not complete or not risk:
        return {name: None for name in fields}
    points = [item.point for item in risk]
    if any(point.status not in {"available", "closed"} for point in points):
        return {name: None for name in fields}
    timestamps = [item.coordinate_timestamp for item in risk]
    if any(value is None for value in timestamps):
        return {name: None for name in fields}
    collateral_price = next((item.collateral_price for item in risk
                             if item.collateral_price), None)
    last_price = next((item.collateral_price for item in reversed(risk)
                       if item.collateral_price), None)
    if collateral_price is None or last_price is None:
        return {name: None for name in fields}
    if (initial.cash_eth_wei or final.cash_eth_wei or
            any(record.eth_execution_fee_wei for record in orders)) and any(
                item.eth_price is None for item in risk):
        return {name: None for name in fields}
    first_eth_price = next((item.eth_price for item in risk if item.eth_price), 0)
    equity = [item.cash_usdc * (item.collateral_price or last_price)
              + (point.remaining_collateral_usd or 0)
              + item.cash_eth_wei * (item.eth_price or 0)
              for item, point in zip(risk, points)]
    peak = max(initial.cash_usdc * collateral_price
               + initial.collateral_usdc * collateral_price
               + initial.cash_eth_wei * first_eth_price, equity[0])
    drawdown = 0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, peak - value)
    starting = ((initial.cash_usdc + initial.collateral_usdc) * collateral_price
                + initial.cash_eth_wei * first_eth_price)
    net = equity[-1] - starting
    deployed_collateral = initial.collateral_usdc * collateral_price + sum(
        record.usdc_deposit * record.collateral_price
        for record in orders if record.status == "filled_estimate"
        and record.collateral_price is not None
    )
    funding = sum(record.cost_breakdown.get("funding_fee_amount", 0)
                  for record in orders if record.status == "filled_estimate")
    borrowing = sum(record.cost_breakdown.get("borrowing_fee_usd", 0)
                    for record in orders if record.status == "filled_estimate")
    impact = sum(int(record.impact.get("total_impact_usd", 0))
                 for record in orders if record.status == "filled_estimate")
    fees = sum(record.cost_breakdown.get("position_fee_amount", 0)
               for record in orders if record.status == "filled_estimate")
    total_cost = sum(record.cost_breakdown.get("total_cost_amount", 0)
                     for record in orders if record.status == "filled_estimate")
    seconds = 0
    for earlier, later in zip(risk, risk[1:]):
        if (earlier.point.buffer_usd is not None
                and earlier.point.buffer_usd <= scenario.liquidation_buffer_usd):
            delta = later.coordinate_timestamp - earlier.coordinate_timestamp
            if delta < 0:
                return {name: None for name in fields}
            seconds += delta
    return {
        "net_pnl_usd": net,
        "collateral_return_factor": (
            net * 10**30 // deployed_collateral
            if deployed_collateral > 0 else None),
        "peak_notional_usd": max([initial.size_usd] + [
            record.ledger_after["size_usd"] for record in orders
            if record.ledger_after is not None]),
        "peak_leverage_factor": max((p.leverage_factor or 0 for p in points), default=0),
        "max_drawdown_usd": drawdown,
        "funding_amount_usdc": funding,
        "borrowing_fee_usd": borrowing,
        "impact_usd": impact,
        "position_fees_usdc": fees,
        "total_position_cost_usdc": total_cost,
        "execution_fees_wei": final.execution_fees_wei - initial.execution_fees_wei,
        "liquidation_count": sum(p.liquidatable is True for p in points),
        "seconds_in_liquidation_buffer": seconds,
    }


def run_scenario(
    evidence: ScenarioEvidence,
    plans: tuple[PlannedOrder, ...],
    scenario: Scenario,
    initial: PositionLedger,
    *,
    risk_coordinates: tuple[Coordinate, ...],
    risk_configuration: Mapping[Coordinate, RiskConfiguration],
    risk_virtual_inventory: Mapping[Coordinate, int],
    risk_referral: Mapping[Coordinate, RiskReferralEvidence],
    timestamps: Mapping[int, int],
) -> ScenarioReport:
    """Run a fixed request stream; a scheduler candidate never implies a fill."""
    reasons: list[str] = []
    records: list[OrderRecord] = []
    risk: list[RiskRecord] = []
    ledger, market = initial, CounterfactualBook()
    try:
        opportunities = evidence.keeper_opportunities()
    except UnavailableEvidence as error:
        return ScenarioReport(scenario, "unavailable", tuple(
            _empty_record(plan, "unavailable", str(error)) for plan in plans
        ), (), _metrics(scenario, [], [], initial, ledger, False), (str(error),))
    candidates = []
    for plan in plans:
        scheduled = schedule(
            plan.scheduling, opportunities,
            inclusion_delay_blocks=scenario.inclusion_delay_blocks,
            keeper_delay_blocks=scenario.keeper_delay_blocks,
            cancellation_requested_block=plan.cancellation_requested_block,
            cancellation_delay_blocks=plan.cancellation_delay_blocks,
        )
        if scheduled.status != "candidate":
            records.append(_empty_record(plan, scheduled.status, None))
            if scheduled.status == "unavailable":
                reasons.append("request schedule lacks trigger evidence")
            continue
        opportunity = scheduled.attempt.opportunity
        coordinate = (opportunity.block, opportunity.transaction_index,
                      opportunity.log_index)
        candidates.append((coordinate, plan, opportunity))
    candidates.sort(key=lambda item: item[0])
    if len({item[0] for item in candidates}) != len(candidates):
        reasons.append("multiple hypothetical orders share keeper coordinate")
        for _, plan, opportunity in candidates:
            records.append(_empty_record(plan, "unavailable", reasons[-1], opportunity))
        candidates = []
    risk_coords = tuple(sorted(risk_coordinates))
    authoritative_risk_coords = tuple(sorted(evidence.required_risk_coordinates()))
    if risk_coords != authoritative_risk_coords:
        reasons.append("declared risk coverage differs from recorded state changes")
    if not risk_coords or len(set(risk_coords)) != len(risk_coords):
        reasons.append("missing or duplicate declared risk coverage")
    if candidates and (not risk_coords or risk_coords[-1] < candidates[-1][0]):
        reasons.append("risk coverage ends before final keeper candidate")
    actions = [(coordinate, 0, plan, opportunity)
               for coordinate, plan, opportunity in candidates]
    actions += [(coordinate, 1, None, None) for coordinate in risk_coords]
    halted = False
    for coordinate, kind, plan, opportunity in sorted(actions):
        if halted:
            if plan is not None:
                records.append(_empty_record(plan, "unavailable",
                                             "earlier liquidation or risk gap", opportunity))
            continue
        if kind == 0:
            state = None
            modeled = None
            try:
                raw = evidence.state_for_opportunity(opportunity)
                state = market.apply_to(raw)
                # Check the open position before a pending stop or decrease can
                # execute. Missing coverage prevents a hypothetical fill.
                if ledger.size_usd:
                    point = assess(state, ledger, risk_configuration.get(coordinate),
                                   virtual_inventory_tokens=risk_virtual_inventory.get(coordinate),
                                   referral=risk_referral.get(coordinate))
                    if point.status == "unavailable":
                        raise UnavailableEvidence(point.reason or "risk unavailable")
                    if point.liquidatable:
                        raise UnavailableEvidence("liquidation before pending order")
                selected_acceptable = (scenario.acceptable_price_override
                                       if scenario.acceptable_price_override is not None
                                       else plan.economics.acceptable_price)
                order = replace(plan.economics,
                                acceptable_price=selected_acceptable,
                                execution_fee_wei=scenario.execution_fee_wei,
                                virtual_inventory_tokens=risk_virtual_inventory.get(coordinate))
                modeled = calculate(state, order, ledger.before(), plan.referral)
                if modeled.status == "unavailable":
                    raise UnavailableEvidence(modeled.reason or "economics unavailable")
                if modeled.status != "eligible":
                    records.append(_empty_record(plan, modeled.status, None,
                                                 opportunity, state, modeled))
                    continue
                effect = plan.pool_effect
                if effect is None:
                    effect = (fee_only_increase_pool_effect(state, order, modeled)
                              if order.is_increase else
                              fee_only_decrease_pool_effect(state, order, modeled))
                transition = apply_fill(ledger, market, raw, order, modeled, effect)
                ledger, market = transition.ledger, transition.market
                records.append(OrderRecord(
                    plan.request_coordinate, coordinate, state.coordinate,
                    "filled_estimate", None, dict(state.configuration),
                    modeled.impact, modeled.fees, modeled.execution_price,
                    transition.collateral_deposit_usdc,
                    transition.collateral_release_usdc,
                    transition.execution_fee_wei, asdict(ledger),
                    dict(state.accrual), state.oracle[ledger.collateral_token][0],
                ))
            except (UnavailableEvidence, KeyError, ValueError) as error:
                reason = str(error)
                reasons.append(reason)
                records.append(_empty_record(plan, "unavailable", reason,
                                             opportunity, state, modeled))
        else:
            try:
                raw = evidence.at(coordinate)
                state = market.apply_to(raw)
                point = assess(state, ledger, risk_configuration.get(coordinate),
                               virtual_inventory_tokens=risk_virtual_inventory.get(coordinate),
                               referral=risk_referral.get(coordinate))
                risk.append(RiskRecord(point, ledger.cash_usdc, ledger.size_usd,
                                       timestamps.get(coordinate[0]),
                                       raw.oracle.get(ledger.collateral_token,
                                                      (None, None))[0],
                                       risk_configuration.get(coordinate),
                                       (risk_referral[coordinate].source
                                        if coordinate in risk_referral else None),
                                       ledger.cash_eth_wei, ledger.collateral_usdc,
                                       asdict(ledger),
                                       raw.oracle.get(raw.index_token, (None, None))[0]))
                if point.status == "unavailable":
                    reasons.append(point.reason or "risk point unavailable")
                    halted = ledger.size_usd > 0
                elif point.liquidatable:
                    reasons.append("liquidation settlement unavailable")
                    halted = True
            except (UnavailableEvidence, KeyError, ValueError) as error:
                reason = str(error)
                reasons.append(reason)
                risk.append(RiskRecord(RiskPoint(coordinate, "unavailable", reason),
                                       ledger.cash_usdc, ledger.size_usd,
                                       timestamps.get(coordinate[0]), None,
                                       cash_eth_wei=ledger.cash_eth_wei,
                                       collateral_usdc=ledger.collateral_usdc,
                                       ledger_snapshot=asdict(ledger)))
                halted = ledger.size_usd > 0
    records.sort(key=lambda record: record.request_coordinate)
    complete = not reasons and len(risk) == len(risk_coords)
    if any(item.coordinate_timestamp is None for item in risk):
        reasons.append("missing risk timestamp")
        complete = False
    if (initial.cash_eth_wei or ledger.cash_eth_wei or
            any(record.eth_execution_fee_wei for record in records)) and any(
                item.eth_price is None for item in risk):
        reasons.append("missing ETH oracle for execution-fee cash valuation")
        complete = False
    metrics = _metrics(scenario, records, risk, initial, ledger, complete)
    return ScenarioReport(scenario, "complete" if complete else "unavailable",
                          tuple(records), tuple(risk), metrics, tuple(reasons))


def run_grid(
    evidence: ScenarioEvidence,
    scenarios: tuple[Scenario, ...],
    request_factory: Callable[[Mapping[str, int | str | bool]], tuple[PlannedOrder, ...]],
    initial: PositionLedger,
    **risk_inputs,
) -> tuple[ScenarioReport, ...]:
    """Generate each predeclared strategy stream and replay every scenario."""
    return tuple(run_scenario(evidence, request_factory(scenario.strategy_parameters),
                              scenario, initial, **risk_inputs)
                 for scenario in scenarios)
