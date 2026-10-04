"""Independent candidate economics use only historical inputs."""

import unittest

from gmx_crypto_bot_v2.domain.referral import ZERO_CODE
from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.simulation.economics import (
    EconomicOrder, PositionBefore, ReferralTerms, calculate,
    compare_validated_order,
)
from gmx_crypto_bot_v2.simulation.evidence import EvidenceState

P = 10**30
M = "0x" + "11" * 20
I = "0x" + "22" * 20
L = "0x" + "33" * 20
S = "0x" + "44" * 20
REF = ReferralTerms(ZERO_CODE, 0, 0, 0, 0, 0)


def state(long=100, short=100, impact_factor=0, borrowing=0, funding=0):
    impact = {
        "position_impact_factor_positive": impact_factor,
        "position_impact_factor_negative": impact_factor,
        "position_impact_exponent_factor_positive": P,
        "position_impact_exponent_factor_negative": P,
        "max_position_impact_factor_positive": P,
        "max_position_impact_factor_negative": P,
        "max_lendable_impact_factor": 0,
        "max_lendable_impact_usd": 0,
    }
    fees = {
        "position_fee_positive": P // 100,
        "position_fee_negative": P // 100,
        "position_fee_receiver": 0,
        "borrowing_fee_receiver": 0,
        "max_ui_fee": 0,
    }
    configuration = {**{"impact:" + k: v for k, v in impact.items()},
                     **{"fee:" + k: v for k, v in fees.items()}}
    accrual = {
        key("CUMULATIVE_BORROWING_FACTOR", M, True): borrowing,
        key("CUMULATIVE_BORROWING_FACTOR", M, False): borrowing,
        key("FUNDING_FEE_AMOUNT_PER_SIZE", M, S, True): funding,
        key("FUNDING_FEE_AMOUNT_PER_SIZE", M, S, False): funding,
    }
    oracle = {I: (P, P), L: (P, P), S: (P, P)}
    return EvidenceState((10, 0, 5), "0xtx", oracle, configuration, accrual,
                         {"long": long * P, "short": short * P},
                         {"long": long, "short": short},
                         {L: 1000, S: 1000}, 100, M, I, L, S)


def order(increase=True, long=True, size=10, acceptable=None, fee=123):
    return EconomicOrder(increase, long, size * P, size, S, acceptable,
                         virtual_inventory_tokens=0, execution_fee_wei=fee)


class EconomicsTest(unittest.TestCase):
    def test_zero_impact_fee_and_separate_eth_execution_payment(self):
        result = calculate(state(), order(), PositionBefore(0, 0, 100, 0, 0), REF)
        self.assertEqual(result.status, "eligible")
        self.assertEqual(result.execution_price, P)
        self.assertEqual(result.fees["position_fee_amount"], 10 * P // 100 // P)
        self.assertEqual(result.execution_fee_wei, 123)
        self.assertEqual(result.fees["total_cost_amount"], 0)

    def test_positive_and_negative_impact_and_acceptable_constraint(self):
        improving = calculate(state(long=80, short=100, impact_factor=P // 10),
                              order(acceptable=P // 2), PositionBefore(0, 0, 100, 0, 0), REF)
        worsening = calculate(state(long=100, short=80, impact_factor=P // 10),
                              order(), PositionBefore(0, 0, 100, 0, 0), REF)
        self.assertGreater(improving.impact["current_impact_usd"], 0)
        self.assertLess(worsening.impact["current_impact_usd"], 0)
        self.assertEqual(improving.status, "acceptable_price_rejected")
        self.assertIsNone(improving.settlement)

    def test_financing_and_full_decrease_settlement(self):
        before = PositionBefore(10 * P, 10, 100, 0, 0)
        result = calculate(state(borrowing=P // 10, funding=10**14),
                           order(increase=False), before, REF)
        self.assertEqual(result.status, "eligible")
        self.assertEqual(result.fees["borrowing_fee_usd"], P)
        self.assertEqual(result.fees["funding_fee_amount"], 1)
        self.assertTrue(result.settlement["full_close"])
        self.assertEqual(result.settlement["remaining_collateral"], 0)
        self.assertEqual(result.settlement["collateral_output"], 98)
        self.assertEqual([p["step"] for p in result.settlement["payments"]],
                         ["funding", "pnl", "fees", "impact", "diff"])

    def test_partial_decrease_withdrawal_preserves_collateral(self):
        candidate = EconomicOrder(False, False, 5 * P, 5, S, None, 0,
                                  withdrawal_amount=7)
        result = calculate(state(), candidate,
                           PositionBefore(10 * P, 10, 100, 0, 0), REF)
        self.assertEqual(result.status, "eligible")
        self.assertFalse(result.settlement["full_close"])
        self.assertEqual(result.settlement["remaining_collateral"], 93)
        self.assertEqual(result.settlement["collateral_output"], 7)

    def test_decrease_tokens_follow_long_ceil_short_floor_and_full_close(self):
        before = PositionBefore(10 * P, 9, 100, 0, 0)
        long_partial = EconomicOrder(False, True, 5 * P, 5, S, None, 0)
        short_partial = EconomicOrder(False, False, 5 * P, 4, S, None, 0)
        self.assertEqual(calculate(state(), long_partial, before, REF).status,
                         "eligible")
        short_state = state()
        short_state.oracle[I] = (2 * P, 2 * P)
        self.assertEqual(calculate(short_state, short_partial, before, REF).status,
                         "eligible")
        wrong = EconomicOrder(False, True, 5 * P, 4, S, None, 0)
        self.assertIn("rounding", calculate(state(), wrong, before, REF).reason)
        incomplete_full = EconomicOrder(False, True, 10 * P, 8, S, None, 0)
        self.assertEqual(calculate(state(), incomplete_full, before, REF).status,
                         "unavailable")

    def test_profitable_decrease_and_liquidation_are_unavailable(self):
        profitable = state()
        profitable.oracle[I] = (2 * P, 2 * P)
        before = PositionBefore(10 * P, 10, 100, 0, 0)
        result = calculate(profitable, order(increase=False), before, REF)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("PnL cap", result.reason)
        liquidation = EconomicOrder(False, True, 10 * P, 10, S, None, 0,
                                    is_liquidation=True)
        result = calculate(state(), liquidation, before, REF)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("liquidation_fee_factor", result.reason)

    def test_insolvent_liquidation_settles_until_first_unpaid_cost(self):
        adverse = state()
        adverse.oracle[I] = (P // 2, P // 2)
        adverse.configuration["risk:liquidation_fee_factor"] = P // 100
        adverse.configuration["risk:max_position_impact_factor_for_liquidations"] = 0
        liquidation = EconomicOrder(False, True, 10 * P, 10, S, None, 0,
                                    is_liquidation=True, execution_fee_wei=0)
        before = PositionBefore(10 * P, 10, 2, 0, 0)
        result = calculate(adverse, liquidation, before, REF)
        self.assertEqual(result.status, "eligible")
        self.assertEqual(result.settlement["insolvent_step"], "pnl")
        self.assertEqual(result.settlement["collateral_output"], 0)
        self.assertEqual([item["step"] for item in result.settlement["payments"]],
                         ["funding", "pnl"])

    def test_profitable_decrease_with_historical_cap(self):
        profitable = state()
        profitable.oracle[I] = (2 * P, 2 * P)
        profitable.configuration["risk:max_pnl_factor_for_traders_long"] = P
        before = PositionBefore(10 * P, 10, 100, 0, 0)
        result = calculate(profitable, order(increase=False), before, REF)
        self.assertEqual(result.status, "eligible")
        self.assertEqual(result.settlement["base_pnl_usd"], 10 * P)
        self.assertEqual(result.settlement["pnl_token_output"], 10)
        self.assertEqual(result.settlement["collateral_output"], 100)
        profitable.configuration["risk:max_pnl_factor_for_traders_long"] = P // 100
        capped = calculate(profitable, order(increase=False), before, REF)
        self.assertEqual(capped.status, "eligible")
        self.assertEqual(capped.settlement["base_pnl_usd"], P)
        self.assertEqual(capped.settlement["pnl_token_output"], 1)
        profitable.configuration["risk:max_pnl_factor_for_traders_long"] = 0
        zero_capped = calculate(profitable, order(increase=False), before, REF)
        self.assertEqual(zero_capped.status, "eligible")
        self.assertEqual(zero_capped.settlement["base_pnl_usd"], 0)
        self.assertEqual(zero_capped.settlement["pnl_token_output"], 0)

    def test_missing_virtual_or_accrual_is_unavailable(self):
        candidate = order()
        missing_virtual = EconomicOrder(candidate.is_increase, candidate.is_long,
            candidate.size_delta_usd, candidate.size_delta_tokens, S, None, None)
        self.assertEqual(calculate(state(), missing_virtual,
                         PositionBefore(0, 0, 0, 0, 0), REF).status, "unavailable")
        s = state()
        s.accrual.clear()
        self.assertEqual(calculate(s, candidate,
                         PositionBefore(0, 0, 0, 0, 0), REF).status, "unavailable")

    def test_validator_comparison_reports_mismatch_without_changing_model(self):
        modeled = calculate(state(), order(), PositionBefore(0, 0, 100, 0, 0), REF)
        report = {"observed_execution_events": [
            {"event_name": "PositionIncrease", "values": {
                "executionPrice": modeled.execution_price,
                "pendingPriceImpactUsd": 0}},
            {"event_name": "PositionFeesCollected", "values": {
                "positionFeeAmount": modeled.fees["position_fee_amount"] + 1,
                "borrowingFeeUsd": 0, "fundingFeeAmount": 0,
                "totalCostAmount": modeled.fees["total_cost_amount"]}},
        ]}
        comparison = compare_validated_order(modeled, report)
        self.assertEqual(comparison["execution_price"], "matched")
        self.assertEqual(comparison["position_fee_amount"], "mismatch")
        self.assertEqual(modeled.fees["position_fee_amount"], 0)
        self.assertEqual(compare_validated_order(modeled, {})["economics"],
                         "unavailable")


if __name__ == "__main__":
    unittest.main()
