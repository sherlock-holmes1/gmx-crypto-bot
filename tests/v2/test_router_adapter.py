"""Economic rule adapter computes from a pinned point and rejects bad evidence."""

import unittest
from dataclasses import replace

from gmx_crypto_bot_v2.simulation.router_adapter import (
    EconomicsAcceptablePriceAdapter, PinnedEconomicPoint,
)
from tests.v2.test_simulation_economics import P, M, I, L, S, REF, order, state
from gmx_crypto_bot_v2.simulation.economics import PositionBefore
from gmx_crypto_bot_v2.simulation.router_report import build_report


class Reader:
    def __init__(self, point):
        self.point = point

    def read(self, order_key, block, block_hash):
        return self.point

    def verify_cells(self, point, groups, block, block_hash):
        return True


class RouterAdapterTests(unittest.TestCase):
    def _case(self, acceptable):
        request = {"market": M, "isLong": True, "sizeDeltaUsd": 10 * P,
                   "initialCollateralToken": S, "acceptablePrice": acceptable,
                   "orderType": 2}
        model_order = order(acceptable=acceptable)
        evidence = state()
        point = PinnedEconomicPoint("0x" + "aa" * 32, "0x" + "bb" * 32,
                                    request, evidence, model_order,
                                    PositionBefore(0, 0, 100, 0, 0), REF, 1, 2)
        preflight = {"order_key": point.order_key, "pin_block": 100,
                     "pin_block_hash": point.block_hash, "outcome": "passed_preflight",
                     "on_chain_request": request,
                     "oracle": {"tokens": [I, L, S], "prices": [[P, P]] * 3,
                                "min_timestamp": 1, "max_timestamp": 2}}
        return point, preflight

    def test_computed_rule_agreement_and_disagreement(self):
        point, preflight = self._case(P)
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["status"],
                         "rule_agreement")
        point, preflight = self._case(P // 2)
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["status"],
                         "rule_disagreement")

    def test_malformed_or_incomplete_point_rejected(self):
        point, preflight = self._case(P)
        class Unverified(Reader):
            def verify_cells(self, point, groups, block, block_hash):
                return False
        self.assertEqual(EconomicsAcceptablePriceAdapter(Unverified(point)).evaluate(preflight)["status"],
                         "gmx_preflight_only")
        bad = replace(point, block_hash="0x" + "cc" * 32)
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(bad)).evaluate(preflight)["reason"],
                         "economic_point_identity_mismatch")
        preflight["oracle"]["prices"][0][1] = P + 1
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "economic_oracle_not_equal")

    def test_timestamp_and_order_type_are_required(self):
        point, preflight = self._case(P)
        preflight["oracle"]["max_timestamp"] = 3
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "economic_oracle_timestamp_not_equal")
        point, preflight = self._case(P)
        request = dict(point.request, orderType=4)
        point = replace(point, request=request)
        preflight["on_chain_request"] = request
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "economic_order_not_equal")

    def test_oracle_arrays_reject_truncation_duplicate_order_and_price_mismatch(self):
        point, preflight = self._case(P)
        preflight["oracle"]["prices"] = [[P, P], [P, P]]
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "router_oracle_array_invalid")
        point, preflight = self._case(P)
        preflight["oracle"]["tokens"][1] = I
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "router_oracle_array_invalid")
        point, preflight = self._case(P)
        preflight["oracle"]["tokens"] = [S, L, I]
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "economic_oracle_token_order_not_equal")
        point, preflight = self._case(P)
        preflight["oracle"]["prices"][2] = [P, P + 1]
        self.assertEqual(EconomicsAcceptablePriceAdapter(Reader(point)).evaluate(preflight)["reason"],
                         "economic_oracle_not_equal")

    def test_report_records_narrow_rule_without_full_parity_claim(self):
        point, result = self._case(P)
        class Preflight:
            deployment = type("DeploymentStub", (), {"chain_id": 42161})()
            def run(self, candidate, oracle):
                return result
        candidate = {"order_key": point.order_key, "category": "long_increase",
                     "creation": {"block_number": 100, "transaction_index": 0, "log_index": 0},
                     "proposed_pin_block": 100, "selection_skip_reasons": []}
        report = build_report([candidate], Preflight(), {point.order_key: object()},
                              rule_adapter=EconomicsAcceptablePriceAdapter(Reader(point)))
        row = report["candidates"][0]
        self.assertEqual(row["rule_comparison"]["status"], "rule_agreement")
        self.assertEqual(row["comparison"]["status"], "gmx_preflight_only")
        self.assertFalse(report["completion_gate_met"])


if __name__ == "__main__":
    unittest.main()
