"""Counterfactual effects and fixed-unit position cash stay separate."""

import unittest

from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.simulation.economics import (
    EconomicOrder, EconomicResult, calculate,
)
from gmx_crypto_bot_v2.simulation.evidence import UnavailableEvidence
from gmx_crypto_bot_v2.simulation.ledger import (
    CounterfactualBook, PoolEffect, PositionLedger, apply_fill,
    fee_only_increase_pool_effect, fee_only_decrease_pool_effect,
)
from tests.v2.test_simulation_economics import I, L, M, P, REF, S, state


def observed():
    snapshot = state()
    for token in (L, S):
        snapshot.accrual[key("POOL_AMOUNT", M, token)] = 1000
        for side in (True, False):
            snapshot.accrual[key("OPEN_INTEREST", M, token, side)] = 50 * P
            snapshot.accrual[key("OPEN_INTEREST_IN_TOKENS", M, token, side)] = 50
    return snapshot


def make_order(increase, size=10, withdrawal=0, deposit=0):
    return EconomicOrder(increase, True, size * P, size, S, None, 0,
                         execution_fee_wei=3, withdrawal_amount=withdrawal,
                         collateral_delta_amount=deposit)


class LedgerTest(unittest.TestCase):
    def test_insolvent_liquidation_closes_ledger_and_transfers_paid_loss(self):
        raw = observed()
        raw.oracle[I] = (P // 2, P // 2)
        raw.configuration["risk:liquidation_fee_factor"] = P // 100
        raw.configuration["risk:max_position_impact_factor_for_liquidations"] = 0
        ledger = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                size_tokens=10, collateral_usdc=2)
        order = EconomicOrder(False, True, 10 * P, 10, S, None, 0,
                              is_liquidation=True)
        book = CounterfactualBook()
        modeled = calculate(book.apply_to(raw), order, ledger.before(), REF)
        self.assertEqual(modeled.settlement["insolvent_step"], "pnl")
        effect = fee_only_decrease_pool_effect(raw, order, modeled)
        self.assertEqual(effect.pool_amount[S], 2)
        transition = apply_fill(ledger, book, raw, order, modeled, effect)
        self.assertEqual(transition.ledger.size_usd, 0)
        self.assertEqual(transition.ledger.collateral_usdc, 0)
        self.assertEqual(transition.ledger.cash_usdc, 0)

    def test_increase_partial_decrease_full_close_balance(self):
        raw = observed()
        ledger = PositionLedger(True, S, cash_usdc=100, cash_eth_wei=20)
        book = CounterfactualBook()
        increase = make_order(True, deposit=50)
        modeled = calculate(raw, increase, ledger.before(), REF)
        first = apply_fill(ledger, book, raw, increase, modeled,
                           PoolEffect({S: 2}, 0, "independent model"))
        self.assertEqual(first.ledger.size_usd, 10 * P)
        self.assertEqual(first.ledger.collateral_usdc, 50)
        self.assertEqual(first.ledger.cash_usdc, 50)
        self.assertEqual(first.ledger.cash_eth_wei, 17)
        self.assertEqual(first.market.long_oi_usd, 10 * P)
        self.assertEqual(raw.open_interest_usd["long"], 100 * P)
        self.assertEqual(raw.pool_amount[S], 1000)
        overlaid = first.market.apply_to(raw)
        self.assertEqual(overlaid.open_interest_usd["long"], 110 * P)
        self.assertEqual(overlaid.pool_amount[S], 1002)
        self.assertEqual(overlaid.accrual[key("OPEN_INTEREST", M, S, True)], 60 * P)

        partial = make_order(False, size=5, withdrawal=5)
        second = apply_fill(first.ledger, first.market, raw, partial,
                            calculate(overlaid, partial, first.ledger.before(), REF),
                            PoolEffect({S: -1}, 0, "independent model"))
        self.assertEqual(second.ledger.size_usd, 5 * P)
        self.assertEqual(second.ledger.collateral_usdc, 45)
        self.assertEqual(second.ledger.cash_usdc, 55)
        self.assertEqual(second.market.long_oi_tokens, 5)
        self.assertEqual(second.market.pool_amount[S], 1)

        remaining = second.market.apply_to(raw)
        close = make_order(False, size=5)
        third = apply_fill(second.ledger, second.market, raw, close,
                           calculate(remaining, close, second.ledger.before(), REF),
                           PoolEffect({S: -1}, 0, "independent model"))
        self.assertEqual(third.ledger.size_usd, 0)
        self.assertEqual(third.ledger.size_tokens, 0)
        self.assertEqual(third.ledger.collateral_usdc, 0)
        self.assertEqual(third.ledger.cash_usdc, 100)
        self.assertEqual(third.ledger.cash_eth_wei, 11)
        self.assertEqual(third.ledger.execution_fees_wei, 9)
        self.assertEqual(third.market.long_oi_usd, 0)
        self.assertEqual(third.market.pool_amount[S], 0)
        self.assertEqual(raw.pool_amount[S], 1000)
        with self.assertRaisesRegex(UnavailableEvidence, "twice"):
            third.market.apply_to(remaining)

    def test_missing_pool_effect_or_eth_cash_cannot_fill(self):
        raw = observed()
        ledger = PositionLedger(True, S, cash_usdc=100, cash_eth_wei=0)
        candidate = make_order(True, deposit=50)
        modeled = calculate(raw, candidate, ledger.before(), REF)
        with self.assertRaisesRegex(UnavailableEvidence, "pool effect"):
            apply_fill(ledger, CounterfactualBook(), raw, candidate, modeled, None)
        with self.assertRaisesRegex(UnavailableEvidence, "ETH"):
            apply_fill(ledger, CounterfactualBook(), raw, candidate, modeled,
                       PoolEffect({S: 0}, 0, "independent model"))

    def test_independent_fee_distribution_matches_recorded_rounding(self):
        raw = observed()
        raw.configuration["fee:position_fee_receiver"] = 37 * P // 100
        modeled = EconomicResult("eligible", None, {}, {
            "position_fee_amount": 3_299_048,
            "protocol_fee_amount": 3_299_048,
            "borrowing_fee_amount": 0,
            "discount_amount": 0,
        }, P, True, None, 0)
        effect = fee_only_increase_pool_effect(raw, make_order(True), modeled)
        self.assertEqual(effect.pool_amount[S], 2_078_401)
        self.assertEqual(effect.impact_pool_tokens, 0)

    def test_second_fill_must_use_counterfactual_impact_state(self):
        raw = observed()
        for direction in ("positive", "negative"):
            raw.configuration["impact:position_impact_factor_" + direction] = P // 10_000
            raw.configuration["impact:position_impact_exponent_factor_" + direction] = 2 * P
        ledger = PositionLedger(True, S, cash_usdc=100, cash_eth_wei=20)
        first_order = make_order(True, deposit=30)
        first_model = calculate(raw, first_order, ledger.before(), REF)
        first = apply_fill(ledger, CounterfactualBook(), raw, first_order,
                           first_model, PoolEffect({S: 0}, 0, "modeled"))
        second_order = make_order(True, deposit=30)
        wrong = calculate(raw, second_order, first.ledger.before(), REF)
        overlaid = first.market.apply_to(raw)
        correct = calculate(overlaid, second_order, first.ledger.before(), REF)
        self.assertNotEqual(wrong.impact["current_impact_usd"],
                            correct.impact["current_impact_usd"])
        with self.assertRaisesRegex(UnavailableEvidence, "current market overlay"):
            apply_fill(first.ledger, first.market, raw, second_order, wrong,
                       PoolEffect({S: 0}, 0, "modeled"))
        second = apply_fill(first.ledger, first.market, raw, second_order,
                            correct, PoolEffect({S: 0}, 0, "modeled"))
        self.assertEqual(second.ledger.size_usd, 20 * P)

    def test_decrease_pool_effect_includes_settled_loss(self):
        raw = observed()
        before = PositionLedger(True, S, cash_usdc=0, cash_eth_wei=10,
                                size_usd=10 * P, size_tokens=10,
                                collateral_usdc=100)
        candidate = make_order(False, size=5)
        modeled = calculate(raw, candidate, before.before(), REF)
        effect = fee_only_decrease_pool_effect(raw, candidate, modeled)
        self.assertEqual(effect.pool_amount[S], 0)
        self.assertEqual(effect.impact_pool_tokens, 0)
        losing = observed()
        losing.oracle[I] = (P // 2, P // 2)
        loss_model = calculate(losing, candidate, before.before(), REF)
        loss_effect = fee_only_decrease_pool_effect(losing, candidate, loss_model)
        self.assertEqual(loss_effect.pool_amount[S], 3)
        gaining = observed()
        gaining.oracle[I] = (2 * P, 2 * P)
        gaining.configuration["risk:max_pnl_factor_for_traders_long"] = P
        gain_model = calculate(gaining, candidate, before.before(), REF)
        gain_effect = fee_only_decrease_pool_effect(gaining, candidate, gain_model)
        self.assertEqual(gain_effect.pool_amount[L], -5)

    def test_positive_long_decrease_impact_debits_impact_pool(self):
        raw = observed()
        raw.open_interest_usd["long"] = 110 * P
        raw.open_interest_tokens["long"] = 110
        for direction in ("positive", "negative"):
            raw.configuration["impact:position_impact_factor_" + direction] = P // 2
        ledger = PositionLedger(True, S, 0, 10, size_usd=10 * P,
                                size_tokens=10, collateral_usdc=100)
        candidate = make_order(False, size=5)
        modeled = calculate(raw, candidate, ledger.before(), REF)
        self.assertEqual(modeled.status, "eligible")
        self.assertEqual(modeled.impact["total_impact_usd"], 5 * P // 2)
        effect = fee_only_decrease_pool_effect(raw, candidate, modeled)
        self.assertEqual(effect.impact_pool_tokens, -3)
        self.assertEqual(effect.pool_amount[S], 0)
        self.assertEqual(effect.pool_amount[L], -2)
        transition = apply_fill(ledger, CounterfactualBook(), raw, candidate,
                                modeled, effect)
        self.assertEqual(transition.market.impact_pool_tokens, -3)
        self.assertEqual(transition.ledger.released_pnl_tokens, 2)
        funded = observed()
        funded.accrual[key("FUNDING_FEE_AMOUNT_PER_SIZE", M, S, True)] = 10**15
        funded_model = calculate(funded, candidate, ledger.before(), REF)
        self.assertGreater(funded_model.fees["funding_fee_amount"], 0)
        funding_effect = fee_only_decrease_pool_effect(funded, candidate, funded_model)
        self.assertEqual(funding_effect.pool_amount[S], 0)
        self.assertEqual(funding_effect.impact_pool_tokens, 0)

    def test_negative_impact_increases_pool_and_impact_pool(self):
        raw = observed()
        raw.open_interest_usd["long"] = 90 * P
        raw.open_interest_tokens["long"] = 90
        for direction in ("positive", "negative"):
            raw.configuration["impact:position_impact_factor_" + direction] = P // 2
        ledger = PositionLedger(True, S, 0, 10, size_usd=10 * P,
                                size_tokens=10, collateral_usdc=100)
        candidate = make_order(False, size=5)
        modeled = calculate(raw, candidate, ledger.before(), REF)
        self.assertEqual(modeled.status, "eligible")
        self.assertLess(modeled.impact["total_impact_usd"], 0)
        effect = fee_only_decrease_pool_effect(raw, candidate, modeled)
        paid = next(item["paid_in_collateral"] for item in
                    modeled.settlement["payments"] if item["step"] == "impact")
        self.assertEqual(effect.pool_amount[S], paid)
        self.assertEqual(effect.impact_pool_tokens, paid)
        transition = apply_fill(ledger, CounterfactualBook(), raw, candidate,
                                modeled, effect)
        self.assertEqual(transition.market.impact_pool_tokens, paid)

    def test_observed_market_floor_and_bad_effect_unavailable(self):
        raw = observed()
        overlay = CounterfactualBook(pool_amount={S: -1001})
        with self.assertRaisesRegex(UnavailableEvidence, "below zero"):
            overlay.apply_to(raw)
        with self.assertRaisesRegex(UnavailableEvidence, "storage cell"):
            CounterfactualBook(long_oi_usd=1,
                oi_usd_cells={("0x" + "55" * 20, True): 1}).apply_to(raw)


if __name__ == "__main__":
    unittest.main()
