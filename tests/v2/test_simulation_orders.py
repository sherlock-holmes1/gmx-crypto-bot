import unittest

from gmx_crypto_bot_v2.simulation.orders import (
    KeeperOpportunity,
    OrderKind,
    Request,
    acceptable_price_met,
    schedule,
)


def opportunity(block: int, low: int, high: int) -> KeeperOpportunity:
    return KeeperOpportunity(block, 0, 0, low, high)


class SchedulerTests(unittest.TestCase):
    def test_request_never_fills_at_creation_and_uses_later_oracle(self):
        request = Request(OrderKind.MARKET_INCREASE, True, 10, acceptable_price=105)
        result = schedule(
            request,
            [
                opportunity(10, 99, 100),
                opportunity(11, 100, 106),
                opportunity(12, 101, 104),
            ],
            inclusion_delay_blocks=0,
            keeper_delay_blocks=0,
        )
        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.attempt.opportunity.block, 11)
        self.assertEqual(result.attempt.oracle_price, 106)
        self.assertFalse(acceptable_price_met(request, 106))
        self.assertTrue(acceptable_price_met(request, 104))

    def test_long_stop_can_lose_to_pending_cancellation(self):
        request = Request(OrderKind.STOP_LOSS, True, 10, trigger_price=90)
        result = schedule(
            request,
            [
                KeeperOpportunity(11, 0, 0, 95, 97, 96),
                KeeperOpportunity(12, 0, 0, 89, 91, 89),
            ],
            inclusion_delay_blocks=0,
            keeper_delay_blocks=0,
            cancellation_requested_block=11,
            cancellation_delay_blocks=2,
        )
        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.attempt.opportunity.block, 12)

    def test_cancel_takes_effect_before_later_keeper(self):
        request = Request(OrderKind.MARKET_DECREASE, False, 10)
        result = schedule(
            request,
            [opportunity(12, 90, 95)],
            inclusion_delay_blocks=0,
            keeper_delay_blocks=0,
            cancellation_requested_block=11,
            cancellation_delay_blocks=1,
        )
        self.assertEqual(result.status, "cancelled")

    def test_short_take_profit_uses_upper_close_bound(self):
        request = Request(OrderKind.TAKE_PROFIT, False, 10, trigger_price=90)
        result = schedule(
            request,
            [
                KeeperOpportunity(11, 0, 0, 85, 91, 91),
                KeeperOpportunity(12, 0, 0, 85, 89, 89),
            ],
            inclusion_delay_blocks=0,
            keeper_delay_blocks=0,
        )
        self.assertEqual(result.attempt.opportunity.block, 12)
        self.assertEqual(result.attempt.oracle_price, 89)

    def test_delay_and_boundary_unresolved(self):
        request = Request(OrderKind.MARKET_INCREASE, False, 10)
        result = schedule(
            request,
            [opportunity(11, 90, 95), opportunity(12, 89, 93)],
            inclusion_delay_blocks=2,
            keeper_delay_blocks=1,
        )
        self.assertEqual(result.status, "unresolved")

    def test_conditional_order_with_missing_trigger_observation_is_unavailable(self):
        request = Request(OrderKind.STOP_LOSS, True, 10, trigger_price=90)
        result = schedule(
            request,
            [opportunity(11, 85, 88)],
            inclusion_delay_blocks=0,
            keeper_delay_blocks=0,
        )
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
