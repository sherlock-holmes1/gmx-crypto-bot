"""Scenario candidates become fills only after economics, ledger and risk checks."""

import unittest
from dataclasses import replace

from gmx_crypto_bot_v2.domain.referral import ZERO_CODE
from gmx_crypto_bot_v2.simulation.economics import EconomicOrder, ReferralTerms
from gmx_crypto_bot_v2.simulation.ledger import PositionLedger
from gmx_crypto_bot_v2.simulation.orders import KeeperOpportunity, OrderKind, Request
from gmx_crypto_bot_v2.simulation.risk import RiskConfiguration, RiskReferralEvidence
from gmx_crypto_bot_v2.simulation.scenarios import (
    PlannedOrder, Scenario, run_grid, run_scenario, scenario_grid,
)
from tests.v2.test_simulation_economics import P, S
from tests.v2.test_simulation_ledger import observed


class FakeEvidence:
    def __init__(self):
        self.opportunity = KeeperOpportunity(11, 0, 9, P, P)
        self.pre = replace(observed(), coordinate=(11, 0, 5))
        self.risk = replace(observed(), coordinate=(11, 0, 9))
        self.later = replace(observed(), coordinate=(12, 0, 9))

    def keeper_opportunities(self):
        return [self.opportunity]

    def state_for_opportunity(self, opportunity):
        assert opportunity == self.opportunity
        return self.pre

    def at(self, coordinate):
        return {self.risk.coordinate: self.risk,
                self.later.coordinate: self.later}[coordinate]

    def required_risk_coordinates(self):
        return (self.risk.coordinate, self.later.coordinate)


def plan(acceptable=None):
    return PlannedOrder(
        (10, 0, 1), Request(OrderKind.MARKET_INCREASE, True, 10),
        EconomicOrder(True, True, 10 * P, 10, S, acceptable, 0,
                      collateral_delta_amount=50),
        ReferralTerms(ZERO_CODE, 0, 0, 0, 0, 0),
    )


def scenario(acceptable=None, inclusion=0):
    return Scenario("base", inclusion, 0, 3, acceptable, {"size": 10}, 2 * P)


def risk_inputs():
    coordinates = ((11, 0, 9), (12, 0, 9))
    config = RiskConfiguration((11, 0, 0), 12, 3 * P, 0, 0,
                               "pinned test config", P, 0)
    referral = RiskReferralEvidence((11, 0, 0), 12,
                                    ReferralTerms(ZERO_CODE, 0, 0, 0, 0, 0),
                                    "pinned account test evidence")
    return dict(risk_coordinates=coordinates,
                risk_configuration={c: config for c in coordinates},
                risk_virtual_inventory={c: 0 for c in coordinates},
                risk_referral={c: referral for c in coordinates},
                timestamps={11: 100, 12: 110})


class ScenarioTest(unittest.TestCase):
    def test_later_candidate_fills_with_evidence_costs_and_balanced_cash(self):
        report = run_scenario(FakeEvidence(), (plan(),), scenario(),
            PositionLedger(True, S, 100, 20), **risk_inputs())
        self.assertEqual(report.status, "complete")
        record = report.orders[0]
        self.assertEqual(record.status, "filled_estimate")
        self.assertEqual(record.request_coordinate, (10, 0, 1))
        self.assertEqual(record.keeper_coordinate, (11, 0, 9))
        self.assertEqual(record.state_coordinate, (11, 0, 5))
        self.assertIsNotNone(record.historical_configuration)
        self.assertEqual(record.usdc_deposit, 50)
        self.assertEqual(record.eth_execution_fee_wei, 3)
        self.assertEqual(record.ledger_after["cash_usdc"], 50)
        self.assertEqual(record.ledger_after["cash_eth_wei"], 17)
        self.assertEqual(len(report.risk), 2)
        self.assertEqual(report.metrics["net_pnl_usd"], -3 * P)
        self.assertEqual(report.metrics["collateral_return_factor"],
                         -(3 * P) * P // (50 * P))
        self.assertEqual(report.metrics["peak_notional_usd"], 10 * P)
        self.assertEqual(report.metrics["execution_fees_wei"], 3)
        self.assertEqual(report.risk[0].cash_eth_wei, 17)
        self.assertEqual(report.risk[0].collateral_usdc, 50)
        self.assertEqual(report.risk[0].ledger_snapshot["cash_usdc"], 50)
        self.assertEqual(report.risk[0].eth_price, P)

    def test_acceptable_price_rejection_never_mutates_ledger(self):
        report = run_scenario(FakeEvidence(), (plan(),), scenario(P // 2),
            PositionLedger(True, S, 100, 20), **risk_inputs())
        self.assertEqual(report.orders[0].status, "acceptable_price_rejected")
        self.assertIsNone(report.orders[0].ledger_after)
        self.assertEqual(report.orders[0].eth_execution_fee_wei, 0)

    def test_missing_risk_coverage_is_unavailable(self):
        inputs = risk_inputs()
        inputs["risk_configuration"] = {}
        report = run_scenario(FakeEvidence(), (plan(),), scenario(),
            PositionLedger(True, S, 100, 20), **inputs)
        self.assertEqual(report.status, "unavailable")
        self.assertIsNone(report.metrics["net_pnl_usd"])
        self.assertEqual(report.risk[0].point.status, "unavailable")
        inputs = risk_inputs()
        inputs["risk_coordinates"] = ((11, 0, 9),)
        report = run_scenario(FakeEvidence(), (plan(),), scenario(),
            PositionLedger(True, S, 100, 20), **inputs)
        self.assertEqual(report.status, "unavailable")
        self.assertTrue(any("differs from recorded" in reason
                            for reason in report.unavailable_reasons))

    def test_boundary_and_predeclared_grid(self):
        grid = scenario_grid(inclusion_delays=(0, 2), keeper_delays=(0,),
            execution_fees_wei=(3, 5), acceptable_prices=(None, P // 2),
            strategy_parameters=({"size": 10}, {"size": 20}),
            liquidation_buffer_usd=2 * P)
        self.assertEqual(len(grid), 16)
        reports = run_grid(FakeEvidence(), grid, lambda _: (plan(),),
            PositionLedger(True, S, 100, 20), **risk_inputs())
        self.assertEqual(len(reports), 16)
        self.assertTrue(any(r.orders[0].status == "unresolved" for r in reports))
        self.assertTrue(any(r.orders[0].status == "acceptable_price_rejected"
                            for r in reports))
        unresolved = next(r for r in reports if r.orders[0].status == "unresolved")
        self.assertEqual(unresolved.metrics["net_pnl_usd"], 0)
        self.assertEqual(unresolved.metrics["peak_notional_usd"], 0)

    def test_gas_scenario_changes_net_pnl_and_missing_eth_oracle_is_unavailable(self):
        evidence = FakeEvidence()
        initial = PositionLedger(True, S, 100, 20)
        low = run_scenario(evidence, (plan(),), scenario(), initial, **risk_inputs())
        expensive = replace(scenario(), execution_fee_wei=5)
        high = run_scenario(evidence, (plan(),), expensive, initial, **risk_inputs())
        self.assertEqual(low.metrics["net_pnl_usd"] - high.metrics["net_pnl_usd"],
                         2 * P)
        missing = FakeEvidence()
        missing.risk.oracle.pop(missing.risk.index_token)
        missing.later.oracle.pop(missing.later.index_token)
        unavailable = run_scenario(missing, (plan(),), scenario(), initial,
                                   **risk_inputs())
        self.assertEqual(unavailable.status, "unavailable")
        self.assertIsNone(unavailable.metrics["net_pnl_usd"])


if __name__ == "__main__":
    unittest.main()
