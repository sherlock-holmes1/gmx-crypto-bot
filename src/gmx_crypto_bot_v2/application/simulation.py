"""Run a bounded, predeclared scenario against a validated recording."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from gmx_crypto_bot_v2.simulation.economics import EconomicOrder
from gmx_crypto_bot_v2.simulation.evidence import EvidenceAdapter, UnavailableEvidence
from gmx_crypto_bot_v2.simulation.ledger import PositionLedger
from gmx_crypto_bot_v2.simulation.orders import KeeperOpportunity, OrderKind, Request, schedule
from gmx_crypto_bot_v2.simulation.scenarios import PlannedOrder, Scenario, run_scenario

Coordinate = tuple[int, int, int]


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not (
        isinstance(value, int) or isinstance(value, str) and value.isdecimal()
    ):
        raise ValueError(f"{label} must be a decimal integer")
    result = int(value)
    if result < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return result


def _coordinate(value: Any, label: str) -> Coordinate:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must be [block, transaction_index, log_index]")
    return tuple(_integer(item, label) for item in value)


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"{label} must be a 20-byte hex address")
    try:
        int(value[2:], 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a 20-byte hex address") from error
    return value.lower()


class _WindowEvidence:
    """Expose only the declared window and its final risk mark to the runner."""

    def __init__(self, adapter: EvidenceAdapter, start: Coordinate, end: Coordinate):
        if start > end or start[0] < adapter.start or end[0] > adapter.end:
            raise ValueError("risk window is outside the recording or reversed")
        self.adapter, self.start, self.end = adapter, start, end
        changes = (coordinate for coordinate in adapter.required_risk_coordinates()
                   if start <= coordinate <= end)
        self._risk_coordinates = tuple(sorted({*changes, end}))

    def keeper_opportunities(self) -> list[KeeperOpportunity]:
        result = []
        for event in self.adapter.events:
            if event["event_name"] != "OrderExecuted":
                continue
            coordinate = self.adapter.coordinate(event)
            if not self.start <= coordinate <= self.end:
                continue
            try:
                state = self.adapter.at(coordinate)
                low, high = state.oracle[self.adapter.tokens[0]]
            except (UnavailableEvidence, KeyError) as error:
                raise UnavailableEvidence(
                    f"keeper opportunity at {coordinate} unavailable: {error}"
                ) from error
            result.append(KeeperOpportunity(*coordinate, low, high))
        return result

    def state_for_opportunity(self, opportunity: KeeperOpportunity):
        return self.adapter.state_for_opportunity(opportunity)

    def at(self, coordinate: Coordinate):
        return self.adapter.at(coordinate)

    def required_risk_coordinates(self) -> tuple[Coordinate, ...]:
        return self._risk_coordinates


def _parse_plan(document: Mapping[str, Any], adapter: EvidenceAdapter):
    account = _address(document.get("account"), "account")
    window = _object(document.get("risk_window"), "risk_window")
    start = _coordinate(window.get("from"), "risk_window.from")
    end = _coordinate(window.get("through"), "risk_window.through")
    evidence = _WindowEvidence(adapter, start, end)

    raw_initial = _object(document.get("initial"), "initial")
    is_long = raw_initial.get("is_long")
    if not isinstance(is_long, bool):
        raise ValueError("initial.is_long must be true or false")
    collateral_token = _address(raw_initial.get("collateral_token"),
                                "initial.collateral_token")
    if collateral_token != adapter.tokens[2]:
        raise ValueError("initial.collateral_token must be the market short token (USDC)")
    initial = PositionLedger(
        is_long, collateral_token,
        _integer(raw_initial.get("cash_usdc"), "initial.cash_usdc"),
        _integer(raw_initial.get("cash_eth_wei"), "initial.cash_eth_wei"),
    )

    raw_scenario = _object(document.get("scenario"), "scenario")
    name = raw_scenario.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("scenario.name must be a nonempty string")
    override = raw_scenario.get("acceptable_price_override")
    scenario = Scenario(
        name,
        _integer(raw_scenario.get("inclusion_delay_blocks"),
                 "scenario.inclusion_delay_blocks"),
        _integer(raw_scenario.get("keeper_delay_blocks"),
                 "scenario.keeper_delay_blocks"),
        _integer(raw_scenario.get("execution_fee_wei"),
                 "scenario.execution_fee_wei"),
        None if override is None else _integer(
            override, "scenario.acceptable_price_override", minimum=1),
        {},
        _integer(raw_scenario.get("liquidation_buffer_usd"),
                 "scenario.liquidation_buffer_usd"),
    )

    raw_orders = document.get("orders")
    if not isinstance(raw_orders, list) or not raw_orders:
        raise ValueError("orders must be a nonempty list")
    opportunities = evidence.keeper_opportunities()
    plans = []
    for number, item in enumerate(raw_orders, 1):
        raw = _object(item, f"orders[{number}]")
        prefix = f"orders[{number}]"
        coordinate = _coordinate(raw.get("request_coordinate"),
                                 f"{prefix}.request_coordinate")
        if coordinate >= end or coordinate[0] < adapter.start:
            raise ValueError(f"{prefix}.request_coordinate must precede the window end")
        try:
            kind = OrderKind(raw.get("kind"))
        except ValueError as error:
            raise ValueError(f"{prefix}.kind is not supported") from error
        trigger = raw.get("trigger_price")
        acceptable = raw.get("acceptable_price")
        request = Request(
            kind, is_long, coordinate[0],
            None if trigger is None else _integer(trigger, f"{prefix}.trigger_price", minimum=1),
            None if acceptable is None else _integer(
                acceptable, f"{prefix}.acceptable_price", minimum=1),
        )
        is_increase = kind is OrderKind.MARKET_INCREASE
        order = EconomicOrder(
            is_increase, is_long,
            _integer(raw.get("size_delta_usd"), f"{prefix}.size_delta_usd", minimum=1),
            _integer(raw.get("size_delta_tokens"), f"{prefix}.size_delta_tokens", minimum=1),
            collateral_token, request.acceptable_price, None,
            _address(raw.get("ui_fee_receiver", "0x" + "0" * 40),
                     f"{prefix}.ui_fee_receiver"),
            collateral_delta_amount=_integer(
                raw.get("collateral_delta_amount", 0),
                f"{prefix}.collateral_delta_amount"),
        )
        cancellation = raw.get("cancellation_requested_block")
        cancellation = (None if cancellation is None else _integer(
            cancellation, f"{prefix}.cancellation_requested_block"))
        cancellation_delay = _integer(raw.get("cancellation_delay_blocks", 0),
                                      f"{prefix}.cancellation_delay_blocks")
        selected = schedule(
            request, opportunities,
            inclusion_delay_blocks=scenario.inclusion_delay_blocks,
            keeper_delay_blocks=scenario.keeper_delay_blocks,
            cancellation_requested_block=cancellation,
            cancellation_delay_blocks=cancellation_delay,
        )
        referral = None
        if selected.attempt is not None:
            try:
                state = evidence.state_for_opportunity(selected.attempt.opportunity)
                referral = adapter.referral_at(account, state.coordinate).terms
            except UnavailableEvidence:
                pass  # The runner reports the missing order evidence.
        plans.append(PlannedOrder(coordinate, request, order, referral,
                                  cancellation_requested_block=cancellation,
                                  cancellation_delay_blocks=cancellation_delay))
    return account, evidence, scenario, initial, tuple(plans)


def _risk_inputs(adapter: EvidenceAdapter, evidence: _WindowEvidence,
                 account: str, plans: tuple[PlannedOrder, ...], scenario: Scenario):
    risk_coordinates = evidence.required_risk_coordinates()
    coordinates = set(risk_coordinates)
    opportunities = evidence.keeper_opportunities()
    for plan in plans:
        selected = schedule(
            plan.scheduling, opportunities,
            inclusion_delay_blocks=scenario.inclusion_delay_blocks,
            keeper_delay_blocks=scenario.keeper_delay_blocks,
            cancellation_requested_block=plan.cancellation_requested_block,
            cancellation_delay_blocks=plan.cancellation_delay_blocks,
        )
        if selected.attempt is not None:
            try:
                coordinates.add(evidence.state_for_opportunity(
                    selected.attempt.opportunity).coordinate)
            except UnavailableEvidence:
                pass
    config, virtual, referral = {}, {}, {}
    for coordinate in sorted(coordinates):
        for target, getter in (
            (config, adapter.risk_configuration_at),
            (virtual, adapter.virtual_inventory_at),
            (referral, lambda c: adapter.referral_at(account, c)),
        ):
            try:
                target[coordinate] = getter(coordinate)
            except (UnavailableEvidence, KeyError, IndexError):
                pass  # The runner marks a needed missing input unavailable.
    return dict(risk_coordinates=risk_coordinates, risk_configuration=config,
                risk_virtual_inventory=virtual, risk_referral=referral,
                timestamps=adapter.timestamps)


def run(recording: Path, plan: Path):
    document = _object(json.loads(plan.read_text(encoding="utf-8")), "plan")
    validation = json.loads((recording / "order-validation.json").read_text(encoding="utf-8"))
    adapter = EvidenceAdapter.load(recording, validation)
    account, evidence, scenario, initial, plans = _parse_plan(document, adapter)
    return run_scenario(evidence, plans, scenario, initial,
                        **_risk_inputs(adapter, evidence, account, plans, scenario))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one offline GMX position scenario against a validated recording.")
    parser.add_argument("--recording", required=True, type=Path,
                        help="complete recording directory")
    parser.add_argument("--plan", required=True, type=Path,
                        help="JSON scenario plan")
    parser.add_argument("--output", type=Path,
                        help="write full JSON report here; otherwise print it to stdout")
    args = parser.parse_args(argv)
    try:
        report = run(args.recording, args.plan)
        payload = json.dumps(asdict(report), indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(payload)
        else:
            args.output.write_text(payload, encoding="utf-8")
            print(f"Scenario: {report.scenario.name}")
            print(f"Status: {report.status}")
            print("Orders: " + ", ".join(order.status for order in report.orders))
            if report.unavailable_reasons:
                print("Reasons: " + "; ".join(report.unavailable_reasons))
            print(f"Report: {args.output}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(2, f"gmx-simulate: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
