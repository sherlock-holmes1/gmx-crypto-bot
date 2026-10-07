"""Typed same-order economics adapter; archive snapshot provider is still required."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gmx_crypto_bot_v2.domain.constants import DECREASE_ORDER_TYPES, INCREASE_ORDER_TYPES

from gmx_crypto_bot_v2.simulation.economics import (
    EconomicOrder, PositionBefore, ReferralTerms, calculate,
)
from gmx_crypto_bot_v2.simulation.evidence import EvidenceState


@dataclass(frozen=True)
class PinnedEconomicPoint:
    """Values obtained by an independent pinned archive-state reader."""

    order_key: str
    block_hash: str
    request: dict
    state: EvidenceState
    economic_order: EconomicOrder
    position_before: PositionBefore
    referral: ReferralTerms
    oracle_min_timestamp: int
    oracle_max_timestamp: int


class PinnedPointReader(Protocol):
    def read(self, order_key: str, block: int, block_hash: str) -> PinnedEconomicPoint: ...
    def verify_cells(self, point: PinnedEconomicPoint, groups: tuple[str, ...],
                     block: int, block_hash: str) -> bool: ...


class EconomicsAcceptablePriceAdapter:
    """Compute only the acceptable-price rule using independent economics."""

    name = "economics_acceptable_price_v1"
    required_state_groups = ("oracle", "configuration", "accrual", "open_interest_tokens",
                             "pool_amount", "impact_pool_amount", "position", "referral",
                             "virtual_inventory")

    def __init__(self, reader: PinnedPointReader):
        self.reader = reader

    def evaluate(self, preflight: dict) -> dict:
        only = lambda reason: {"status": "gmx_preflight_only", "reason": reason}
        if preflight.get("outcome") not in {"passed_preflight", "validation_error"}:
            return only("no_decoded_gmx_decision")
        try:
            point = self.reader.read(preflight["order_key"], preflight["pin_block"],
                                     preflight["pin_block_hash"])
        except (KeyError, ValueError, OSError) as error:
            return only(f"pinned_economic_state_unavailable:{error}")
        if point.order_key.lower() != preflight["order_key"].lower() or point.block_hash.lower() != preflight["pin_block_hash"].lower():
            return only("economic_point_identity_mismatch")
        chain_request = preflight.get("on_chain_request")
        if not isinstance(chain_request, dict) or point.request != chain_request:
            return only("economic_request_not_equal")
        try:
            verified = self.reader.verify_cells(point, self.required_state_groups,
                                                preflight["pin_block"], preflight["pin_block_hash"])
        except (AttributeError, OSError, ValueError) as error:
            return only(f"pinned_state_verification_unavailable:{error}")
        if verified is not True:
            return only("required_archive_state_cells_unproved")
        oracle = preflight.get("oracle")
        if not isinstance(oracle, dict):
            return only("router_oracle_input_missing")
        tokens = oracle.get("tokens")
        prices = oracle.get("prices")
        if (not isinstance(tokens, (list, tuple)) or not isinstance(prices, (list, tuple))
                or not tokens or len(tokens) != len(prices)
                or any(not isinstance(token, str) for token in tokens)
                or len({token.lower() for token in tokens}) != len(tokens)
                or any(not isinstance(pair, (list, tuple)) or len(pair) != 2
                       or any(type(value) is not int or value <= 0 for value in pair)
                       or pair[0] > pair[1] for pair in prices)):
            return only("router_oracle_array_invalid")
        expected_prices = {token.lower(): tuple(pair) for token, pair in zip(tokens, prices)}
        if [token.lower() for token in tokens] != [token.lower() for token in point.state.oracle]:
            return only("economic_oracle_token_order_not_equal")
        if expected_prices != {token.lower(): tuple(pair) for token, pair in point.state.oracle.items()}:
            return only("economic_oracle_not_equal")
        if (point.oracle_min_timestamp != oracle.get("min_timestamp") or
                point.oracle_max_timestamp != oracle.get("max_timestamp")):
            return only("economic_oracle_timestamp_not_equal")
        if point.state.market.lower() != str(chain_request.get("market", "")).lower():
            return only("economic_market_not_equal")
        order = point.economic_order
        if (order.is_long != chain_request.get("isLong") or
                order.is_increase != (chain_request.get("orderType") in INCREASE_ORDER_TYPES) or
                chain_request.get("orderType") not in INCREASE_ORDER_TYPES | DECREASE_ORDER_TYPES or
                order.size_delta_usd != chain_request.get("sizeDeltaUsd") or
                order.collateral_token.lower() != str(chain_request.get("initialCollateralToken", "")).lower() or
                order.acceptable_price != chain_request.get("acceptablePrice")):
            return only("economic_order_not_equal")
        modeled = calculate(point.state, order, point.position_before, point.referral)
        if modeled.status == "unavailable" or modeled.acceptable_met is None:
            return only(f"economics_unavailable:{modeled.reason}")
        # A router pass proves this one rule passed. A generic validation error
        # does not identify the failed rule and cannot be compared here.
        if preflight["outcome"] != "passed_preflight":
            return only("gmx_error_rule_not_identified_as_acceptable_price")
        return {"status": "rule_agreement" if modeled.acceptable_met else "rule_disagreement",
                "rule": "acceptable_price", "adapter": self.name,
                "model_status": modeled.status, "model_execution_price": modeled.execution_price,
                "model_acceptable_met": modeled.acceptable_met,
                "model_calculation_digest": modeled.calculation_digest,
                "scope": "rule_only_not_full_execution"}
