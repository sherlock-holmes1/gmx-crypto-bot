"""Stage 3 report gates and failure accounting."""

import unittest

from gmx_crypto_bot_v2.simulation.router_report import REQUEST_FIELDS, build_report, compare


class RouterReportTests(unittest.TestCase):
    def setUp(self):
        self.request = {field: 1 for field in REQUEST_FIELDS}
        self.preflight = {"order_key": "0x" + "aa" * 32, "pin_block_hash": "0x" + "bb" * 32,
                          "outcome": "passed_preflight", "on_chain_request": self.request,
                          "oracle": {"tokens": ["0x" + "cc" * 20], "prices": [[1, 2]]}}
        self.model = {"order_key": self.preflight["order_key"],
                      "pin_block_hash": self.preflight["pin_block_hash"],
                      "adapter": "observed-order-point-v1", "compared_rule": "execution_eligibility",
                      "decision": "pass", "request": self.request.copy(),
                      "oracle": self.preflight["oracle"], "state_scope_complete": True,
                      "required_state_cells": ["position"],
                      "state_cells": {"position": {"archive_source": "block 100 DataStore",
                                                   "model_source": "checkpoint block 100",
                                                   "archive_value": 4, "model_value": 4}}}

    def test_missing_adapter_is_preflight_only(self):
        self.assertEqual(compare(self.preflight, None)["reason"], "registered_execution_adapter_unavailable")

    def test_terminal_outcome_cannot_be_used_as_model_decision(self):
        model = self.model | {"decision": "OrderExecuted"}
        self.assertEqual(compare(self.preflight, model)["status"], "gmx_preflight_only")

    def test_mismatched_state_is_preflight_only(self):
        self.model["state_cells"]["position"]["model_value"] = 5
        self.assertEqual(compare(self.preflight, self.model)["status"], "gmx_preflight_only")

    def test_agreement_requires_full_manifest(self):
        self.assertEqual(compare(self.preflight, self.model)["status"], "gmx_preflight_only")
        self.model["decision"] = "fail"
        self.assertEqual(compare(self.preflight, self.model)["status"], "gmx_preflight_only")

    def test_report_keeps_unavailable_and_category_gap(self):
        candidate = {"order_key": self.preflight["order_key"], "category": "long_increase",
                     "creation": {"block_number": 1, "transaction_index": 0, "log_index": 1},
                     "selection_skip_reasons": [], "proposed_pin_block": 1}
        report = build_report([candidate], None, {}, recording="example")
        self.assertEqual(report["candidates"][0]["preflight"]["outcome"], "provider_failure")
        self.assertFalse(report["completion_gate_met"])
        self.assertEqual(report["coverage"]["long_increase"]["gap"], "no_verified_same_order_comparison")


if __name__ == "__main__":
    unittest.main()
