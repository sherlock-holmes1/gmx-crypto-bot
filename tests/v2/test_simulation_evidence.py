"""Historical evidence must be transaction-scoped and reversible at an order."""

import unittest

from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.domain.keys import FACTOR_FIELDS, MARKET_FIELDS
from gmx_crypto_bot_v2.simulation.evidence import (
    EvidenceAdapter, UnavailableEvidence, observed_ui_receivers,
)

M = "0x" + "11" * 20
I = "0x" + "22" * 20
L = "0x" + "33" * 20
S = "0x" + "44" * 20


class ConstantHistory:
    def at(self, coordinate):
        return 7


def event(name, log, values, tx="0xaaa", block=10):
    return dict(event_name=name, values=values, block_number=block,
                transaction_index=0, log_index=log, transaction_hash=tx)


def fixture(events):
    opening = {}
    for token in (L, S):
        opening[key("POOL_AMOUNT", M, token)] = 100
        for side in (True, False):
            opening[key("OPEN_INTEREST", M, token, side)] = 10
            opening[key("OPEN_INTEREST_IN_TOKENS", M, token, side)] = 5
    return EvidenceAdapter(start=10, end=12, market=M, index_token=I,
        long_token=L, short_token=S, opening=opening, events=events,
        impact_histories={field: ConstantHistory() for field in
            set(FACTOR_FIELDS.values()) | set(MARKET_FIELDS.values())},
        fee_histories={field: ConstantHistory() for field in
            ("position_fee_positive", "position_fee_negative",
             "position_fee_receiver", "borrowing_fee_receiver", "max_ui_fee")})


def prices():
    return [event("OraclePriceUpdate", n, dict(token=t, minPrice=100, maxPrice=110))
            for n, t in enumerate((I, L, S))]


class EvidenceAdapterTest(unittest.TestCase):
    def test_ui_receiver_comes_from_observed_fee_event(self):
        rows = [event("PositionFeesCollected", 1,
                      {"uiFeeReceiver": "0xABC"}),
                event("OrderCreated", 2, {"uiFeeReceiver": "0xWRONG"})]
        self.assertEqual(observed_ui_receivers(rows), {"0xabc"})

    def test_same_transaction_oracle_and_pre_order_oi_and_pool(self):
        rows = prices() + [
            event("PositionImpactPoolAmountUpdated", 3,
                  dict(market=M, delta=2, nextValue=52)),
            event("OpenInterestInTokensUpdated", 4,
                  dict(market=M, collateralToken=S, isLong=True, delta=3, nextValue=8)),
            event("PositionIncrease", 5,
                  dict(market=M, collateralToken=S, isLong=True,
                       sizeDeltaInTokens=3, orderKey="0xorder")),
            event("OrderExecuted", 6, dict(key="0xorder")),
        ]
        adapter = fixture(rows)
        state = adapter.at((10, 0, 5))
        self.assertEqual(state.oracle[I], (100, 110))
        self.assertEqual(state.open_interest_tokens["long"], 10)
        self.assertEqual(state.impact_pool_amount, 50)
        self.assertEqual(state.configuration["impact:position_impact_factor_positive"], 7)
        self.assertEqual(len(adapter.keeper_opportunities()), 1)
        self.assertEqual(adapter.keeper_opportunities()[0].oracle_max, 110)
        self.assertEqual(adapter.state_for_opportunity(
            adapter.keeper_opportunities()[0]).coordinate, (10, 0, 5))

    def test_oracle_cannot_cross_transaction_boundary(self):
        rows = prices() + [event("PositionImpactPoolAmountUpdated", 3,
            dict(market=M, delta=0, nextValue=50)),
            event("OrderExecuted", 4, {}, tx="0xbbb")]
        adapter = fixture(rows)
        with self.assertRaisesRegex(UnavailableEvidence, "same-transaction oracle"):
            adapter.at((10, 0, 4))
        with self.assertRaisesRegex(UnavailableEvidence, "keeper opportunity"):
            adapter.keeper_opportunities()

    def test_unattributed_pool_update_cannot_be_reversed(self):
        rows = prices() + [
            event("PositionImpactPoolAmountUpdated", 3,
                  dict(market=M, delta=0, nextValue=50)),
            event("PoolAmountUpdated", 4,
                  dict(market=M, token=L, delta=2, nextValue=102)),
            event("PositionIncrease", 5,
                  dict(market=M, collateralToken=S, isLong=True,
                       sizeDeltaInTokens=0, sizeDeltaUsd=0, orderKey="0xone")),
            event("OrderExecuted", 6, dict(key="0xother")),
        ]
        with self.assertRaisesRegex(UnavailableEvidence, "attributed"):
            fixture(rows).at((10, 0, 5))

    def test_unique_order_can_attribute_pool_and_usd_oi(self):
        rows = prices() + [
            event("PositionImpactPoolAmountUpdated", 3,
                  dict(market=M, delta=0, nextValue=50)),
            event("OpenInterestUpdated", 4,
                  dict(market=M, collateralToken=S, isLong=True,
                       delta=3, nextValue=13)),
            event("PoolAmountUpdated", 5,
                  dict(market=M, token=L, delta=2, nextValue=102)),
            event("PositionIncrease", 6,
                  dict(market=M, collateralToken=S, isLong=True,
                       sizeDeltaInTokens=0, sizeDeltaUsd=3, orderKey="0xone")),
            event("OrderExecuted", 7, dict(key="0xone")),
        ]
        state = fixture(rows).at((10, 0, 6))
        self.assertEqual(state.open_interest_usd["long"], 20)
        self.assertEqual(state.pool_amount[L], 100)

    def test_missing_config_and_broken_continuity_unavailable(self):
        rows = prices() + [event("PositionImpactPoolAmountUpdated", 3,
            dict(market=M, delta=0, nextValue=50)),
            event("PoolAmountUpdated", 4,
            dict(market=M, token=L, delta=2, nextValue=999)),
            event("OrderExecuted", 5, {})]
        adapter = fixture(rows)
        with self.assertRaisesRegex(UnavailableEvidence, "continuity"):
            adapter.at((10, 0, 5))
        adapter = fixture(rows[:4] + [rows[-1]])
        adapter.fee_histories = {"position_fee_positive": ConstantHistory()}
        with self.assertRaisesRegex(UnavailableEvidence, "configuration"):
            adapter.at((10, 0, 5))

    def test_outside_bounds_and_unrecorded_coordinate_unavailable(self):
        adapter = fixture(prices() + [event("PositionImpactPoolAmountUpdated", 3,
            dict(market=M, delta=0, nextValue=50))])
        with self.assertRaises(UnavailableEvidence):
            adapter.at((13, 0, 1))
        with self.assertRaises(UnavailableEvidence):
            adapter.at((10, 0, 9))


if __name__ == "__main__":
    unittest.main()
