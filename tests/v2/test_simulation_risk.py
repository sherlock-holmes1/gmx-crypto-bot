"""Adverse oracle risk is checked while stops remain pending."""

import unittest
from dataclasses import replace

from gmx_crypto_bot_v2.domain.referral import ZERO_CODE
from gmx_crypto_bot_v2.simulation.economics import ReferralTerms
from gmx_crypto_bot_v2.simulation.ledger import CounterfactualBook, PositionLedger
from gmx_crypto_bot_v2.simulation.risk import (
    RiskConfiguration, RiskReferralEvidence, assess, monitor_path,
)
from tests.v2.test_simulation_economics import I, P, S, state


def covered_state(coordinate=(10, 0, 5), index=P):
    observed = state()
    observed.oracle[I] = (index, index)
    observed = replace(observed, coordinate=coordinate)
    return CounterfactualBook().apply_to(observed)


def config(coordinate=(10, 0, 5)):
    return RiskConfiguration(coordinate, 12, 3 * P, 0, 0,
                             "pinned historical test configuration", P, 0)


def referral(coordinate=(10, 0, 5), pro_factor=0):
    terms = ReferralTerms(ZERO_CODE, 0, 0, 0, int(pro_factor > 0), pro_factor)
    return RiskReferralEvidence(coordinate, 12, terms,
                                "pinned account-specific test evidence")


class RiskTest(unittest.TestCase):
    def test_long_liquidates_before_pending_stop(self):
        safe = covered_state()
        adverse = covered_state((11, 0, 5), P // 2)
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=6)
        first = assess(safe, position, config(), virtual_inventory_tokens=0,
                       referral=referral())
        second = assess(adverse, position, config(), virtual_inventory_tokens=0,
                        referral=referral())
        self.assertFalse(first.liquidatable)
        self.assertTrue(second.liquidatable)
        self.assertEqual(second.adverse_index_price, P // 2)
        self.assertEqual(second.buffer_usd, -2 * P)
        self.assertEqual(second.liquidation_settlement, "unavailable")
        path = monitor_path([safe, adverse], position,
            {safe.coordinate: config(), adverse.coordinate: config()},
            [safe.coordinate, adverse.coordinate],
            virtual_inventory={safe.coordinate: 0, adverse.coordinate: 0},
            referrals={safe.coordinate: referral(), adverse.coordinate: referral()},
            stop_execution=(12, 0, 8))
        self.assertEqual(path.status, "liquidation_before_stop")
        self.assertEqual(path.first_liquidation, adverse.coordinate)

    def test_short_uses_maximum_index_and_minimum_collateral_price(self):
        observed = state()
        observed.oracle[I] = (P, 2 * P)
        observed.oracle[S] = (P // 2, P)
        marked = CounterfactualBook().apply_to(observed)
        position = PositionLedger(False, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=20)
        point = assess(marked, position, config(), virtual_inventory_tokens=0,
                       referral=referral())
        self.assertEqual(point.adverse_index_price, 2 * P)
        self.assertEqual(point.collateral_price, P // 2)
        self.assertEqual(point.remaining_collateral_usd, 0)
        self.assertTrue(point.liquidatable)

    def test_missing_config_or_interval_is_unavailable_not_safe(self):
        safe = covered_state()
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=6)
        self.assertEqual(assess(safe, position, None).status, "unavailable")
        path = monitor_path([safe], position, {safe.coordinate: config()},
                            [safe.coordinate, (11, 0, 5)],
                            virtual_inventory={safe.coordinate: 0},
                            referrals={safe.coordinate: referral()},
                            stop_execution=(12, 0, 8))
        self.assertEqual(path.status, "unavailable")
        self.assertIn("coverage", path.reason)
        expired = RiskConfiguration((9, 0, 0), 9, 3 * P, 0, 0, "stale")
        self.assertEqual(assess(safe, position, expired,
                                virtual_inventory_tokens=0,
                                referral=referral()).status, "unavailable")

    def test_financing_and_pending_impact_reduce_buffer(self):
        marked = covered_state()
        from gmx_crypto_bot_v2.domain.swap_keys import key
        marked.accrual[key("CUMULATIVE_BORROWING_FACTOR", marked.market, True)] = P // 10
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=6,
                                  pending_impact_amount=-1)
        point = assess(marked, position, config(), virtual_inventory_tokens=0,
                       referral=referral())
        self.assertEqual(point.borrowing_usd, P)
        self.assertEqual(point.pending_impact_usd, -P)
        self.assertEqual(point.remaining_collateral_usd, 4 * P)
        self.assertEqual(point.buffer_usd, P)

    def test_full_close_impact_and_fee_prevent_false_safe_mark(self):
        # Closing a minority long worsens OI balance; there is no pending
        # impact, so a pending-only check would call this position safe.
        observed = state(long=80, short=100, impact_factor=P // 10)
        marked = CounterfactualBook().apply_to(observed)
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=3)
        point = assess(marked, position, config(), virtual_inventory_tokens=0,
                       referral=referral())
        self.assertEqual(point.status, "available")
        self.assertLess(point.full_close_impact_usd, 0)
        self.assertTrue(point.liquidatable)

        no_impact = covered_state()
        no_impact.configuration["fee:position_fee_positive"] = P // 10
        no_impact.configuration["fee:position_fee_negative"] = P // 10
        fee_point = assess(no_impact, position, config(), virtual_inventory_tokens=0,
                           referral=referral())
        self.assertEqual(fee_point.close_fee_usd, P)
        self.assertTrue(fee_point.liquidatable)

    def test_zero_equity_liquidates_even_with_zero_threshold(self):
        marked = covered_state()
        zero_requirement = RiskConfiguration((10, 0, 5), 12, 0, 0, 0,
            "historical test", P, 0)
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=0)
        point = assess(marked, position, zero_requirement,
                       virtual_inventory_tokens=0, referral=referral())
        self.assertEqual(point.remaining_collateral_usd, 0)
        self.assertEqual(point.requirement_usd, 0)
        self.assertTrue(point.liquidatable)

    def test_missing_full_close_inputs_are_unavailable(self):
        marked = covered_state()
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=6)
        self.assertEqual(assess(marked, position, config()).status, "unavailable")
        incomplete = RiskConfiguration((10, 0, 5), 12, 3 * P, 0, 0,
                                       "missing liquidation impact")
        self.assertEqual(assess(marked, position, incomplete,
                                virtual_inventory_tokens=0,
                                referral=referral()).status, "unavailable")

    def test_profitable_mark_without_max_pnl_cap_is_unavailable(self):
        profitable = covered_state(index=2 * P)
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=6)
        point = assess(profitable, position, config(), virtual_inventory_tokens=0,
                       referral=referral())
        self.assertEqual(point.status, "unavailable")
        self.assertIn("max-PnL", point.reason)

    def test_borrowing_rounds_to_collateral_tokens_before_usd(self):
        marked = covered_state()
        from gmx_crypto_bot_v2.domain.swap_keys import key
        marked.oracle[S] = (3 * P, 3 * P)
        marked.accrual[key("CUMULATIVE_BORROWING_FACTOR", marked.market, True)] = P // 5
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=1)
        threshold = RiskConfiguration((10, 0, 5), 12, 2 * P, 0, 0,
                                      "historical test", P, 0)
        point = assess(marked, position, threshold, virtual_inventory_tokens=0,
                       referral=referral())
        self.assertEqual(point.status, "available")
        self.assertEqual(point.borrowing_usd, 0)
        self.assertEqual(point.remaining_collateral_usd, 3 * P)
        self.assertFalse(point.liquidatable)

    def test_liquidation_check_ignores_nonzero_ui_receiver(self):
        marked = covered_state()
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=3)
        with_ui = RiskConfiguration((10, 0, 5), 12, 3 * P, 0, 0,
                                    "historical test", P, P)
        point = assess(marked, position, with_ui, virtual_inventory_tokens=0,
                       referral=referral())
        self.assertEqual(point.close_fee_usd, 0)
        self.assertFalse(point.liquidatable)

    def test_referral_discount_changes_liquidation_boundary(self):
        marked = covered_state()
        marked.configuration["fee:position_fee_positive"] = P // 10
        marked.configuration["fee:position_fee_negative"] = P // 10
        position = PositionLedger(True, S, 0, 0, size_usd=10 * P,
                                  size_tokens=10, collateral_usdc=3)
        undiscounted = assess(marked, position, config(),
                              virtual_inventory_tokens=0, referral=referral())
        discounted = assess(marked, position, config(),
                            virtual_inventory_tokens=0,
                            referral=referral(pro_factor=P))
        self.assertTrue(undiscounted.liquidatable)
        self.assertEqual(undiscounted.close_fee_usd, P)
        self.assertFalse(discounted.liquidatable)
        self.assertEqual(discounted.close_discount_amount, 1)
        self.assertEqual(discounted.close_fee_usd, 0)
        self.assertEqual(assess(marked, position, config(),
                         virtual_inventory_tokens=0).status, "unavailable")


if __name__ == "__main__":
    unittest.main()
