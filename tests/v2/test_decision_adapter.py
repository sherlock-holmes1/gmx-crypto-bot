import json
import tempfile
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.decision_adapter import SelectedIncreaseDecisionAdapter


ROOT = Path(__file__).resolve().parents[2] / "evidence/step4-1"


class DecisionAdapterTests(unittest.TestCase):
    def adapter(self, oracle="selected-order-creation-oracle.json", config=None, risk=None,
                balance=None, fee_clocks=None, borrowing_skip=None,
                funding_selector=None):
        return SelectedIncreaseDecisionAdapter(
            ROOT / "long-increase-archive-sidecar.json", ROOT / "sourcify-source-proof.json",
            ROOT / "historical-source-manifest.json", ROOT / oracle,
            ROOT / "pinned-increase-executor.json", ROOT / "sourcify-v2/increase_executor.json",
            ROOT / "pinned-swap-handler.json", ROOT / "sourcify-v2/swap_handler.json",
            config, risk, balance, fee_clocks, borrowing_skip, funding_selector)

    def configured_adapter(self):
        return self.adapter(config=ROOT / "pinned-decision-config.json")

    def risk_adapter(self, risk=None):
        return self.adapter(config=ROOT / "pinned-decision-config.json",
                            risk=risk or ROOT / "pinned-risk-cells.json")

    def balance_adapter(self, balance=None, fee_clocks=None, borrowing_skip=None,
                        funding_selector=None):
        return self.adapter(config=ROOT / "pinned-decision-config.json",
                            risk=ROOT / "pinned-risk-cells.json",
                            balance=balance or ROOT / "pinned-balance-inputs.json",
                            fee_clocks=fee_clocks, borrowing_skip=borrowing_skip,
                            funding_selector=funding_selector)

    def changed_risk(self, change):
        record = json.loads((ROOT / "pinned-risk-cells.json").read_text())
        change(record)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "risk.json"
        path.write_text(json.dumps(record))
        self.addCleanup(directory.cleanup)
        return path

    def changed_balance(self, change):
        record = json.loads((ROOT / "pinned-balance-inputs.json").read_text())
        change(record)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "balance.json"
        path.write_text(json.dumps(record))
        self.addCleanup(directory.cleanup)
        return path

    def changed_fee_clocks(self, change):
        record = json.loads((ROOT / "pinned-fee-clocks.json").read_text())
        change(record)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "fee-clocks.json"
        path.write_text(json.dumps(record))
        self.addCleanup(directory.cleanup)
        return path

    def changed_borrowing_skip(self, change):
        record = json.loads((ROOT / "pinned-borrowing-skip.json").read_text())
        change(record)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "borrowing-skip.json"
        path.write_text(json.dumps(record))
        self.addCleanup(directory.cleanup)
        return path

    def changed_funding_selector(self, change):
        record = json.loads((ROOT / "pinned-funding-selector.json").read_text())
        change(record)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "funding-selector.json"
        path.write_text(json.dumps(record))
        self.addCleanup(directory.cleanup)
        return path

    def tampered_config(self, change):
        config = json.loads((ROOT / "pinned-decision-config.json").read_text())
        change(config)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(config))
        self.addCleanup(directory.cleanup)
        return path

    def preflight(self, adapter):
        sidecar, oracle = adapter.sidecar, adapter.oracle
        return {"outcome": "passed_preflight", "order_key": oracle["order_key"],
                "pin_block": sidecar["pin"]["number"],
                "pin_block_hash": sidecar["pin"]["recorded_hash"],
                "pin_block_hash_after": sidecar["pin"]["recorded_hash"],
                "on_chain_request": sidecar["pinned_order"],
                "oracle": {"tokens": oracle["tokens"], "prices": oracle["prices"],
                           "min_timestamp": oracle["min_timestamp"],
                           "max_timestamp": oracle["max_timestamp"]}}

    def test_creation_counterfactual_needs_full_rule_coverage(self):
        adapter = self.adapter()
        decision = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(decision["status"], "gmx_preflight_only")
        self.assertEqual(decision["reason"], "independent_market_increase_rule_coverage_incomplete")
        self.assertIn("reserve_and_open_interest_reserve", decision["missing_rule_evidence"])
        self.assertIn("zero_swap_path_input_conditions", decision["proved_branches"])
        self.assertIn("collateral_token_membership", decision["proved_branches"])

    def test_pinned_config_proves_expiration_and_enabled_market_but_not_agreement(self):
        adapter = self.configured_adapter()
        decision = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(adapter.decision_config["values"], {
            "request_expiration_time": 300, "is_market_disabled": False})
        self.assertEqual(decision["status"], "gmx_preflight_only")
        self.assertIn("order_valid_from_and_expiration", decision["proved_branches"])
        self.assertIn("market_and_collateral_token_valid", decision["proved_branches"])
        self.assertNotIn("order_valid_from_and_expiration", decision["missing_rule_evidence"])
        self.assertIn("reserve_and_open_interest_reserve", decision["missing_rule_evidence"])

    def test_expiration_uses_max_oracle_timestamp_with_inclusive_boundary(self):
        adapter = self.configured_adapter()
        expiry = adapter.sidecar["pinned_order"]["updatedAtTime"] + 300
        adapter.oracle["max_timestamp"] = expiry
        preflight = self.preflight(adapter)
        self.assertIn("order_valid_from_and_expiration",
                      adapter.evaluate(preflight)["proved_branches"])
        adapter.oracle["max_timestamp"] = expiry + 1
        preflight = self.preflight(adapter)
        result = adapter.evaluate(preflight)
        self.assertEqual(result["reason"], "independent_request_expired")
        self.assertEqual(result["model_result"], "reject")
        self.assertEqual(result["status"], "gmx_preflight_only")

    def test_market_increase_valid_from_is_exempt_at_both_sides_of_boundary(self):
        adapter = self.configured_adapter()
        minimum = adapter.oracle["min_timestamp"]
        order = adapter.sidecar["pinned_order"]
        self.assertEqual(order["orderType"], 2)
        order["validFromTime"] = minimum + 1
        below = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(below["status"], "gmx_preflight_only")
        self.assertIn("market_order_valid_from_exemption", below["proved_branches"])
        self.assertIn("order_valid_from_and_expiration", below["proved_branches"])
        order["validFromTime"] = minimum
        equal = adapter.evaluate(self.preflight(adapter))
        self.assertIn("order_valid_from_and_expiration", equal["proved_branches"])
        self.assertEqual(equal["reason"], "independent_market_increase_rule_coverage_incomplete")

    def test_pinned_risk_cells_prove_min_size_max_oi_and_both_reserves_only(self):
        adapter = self.risk_adapter()
        decision = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(decision["status"], "gmx_preflight_only")
        self.assertEqual(decision["reason"], "independent_market_increase_rule_coverage_incomplete")
        for branch in ("minimum_position_size_usd", "max_open_interest",
                       "reserve_and_open_interest_reserve"):
            self.assertIn(branch, decision["proved_branches"])
        self.assertIn("position_fees_and_collateral_sufficiency",
                      decision["missing_rule_evidence"])
        self.assertIn("minimum_position_and_collateral", decision["missing_rule_evidence"])
        self.assertIn("market_token_balances", decision["missing_rule_evidence"])
        self.assertIsNotNone(decision["risk_cells_sha256"])

    def test_min_position_size_and_max_oi_rejections_are_independent(self):
        adapter = self.risk_adapter()
        next_size = (adapter.sidecar["position"]["value"]["sizeInUsd"] +
                     adapter.sidecar["pinned_order"]["sizeDeltaUsd"])
        adapter.risk_cells["values"]["min_position_size_usd"] = next_size + 1
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "independent_min_position_size_failed")
        self.assertEqual(result["status"], "gmx_preflight_only")
        adapter.risk_cells["values"]["min_position_size_usd"] = next_size
        adapter.risk_cells["values"]["max_open_interest_long"] = 0
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "independent_max_open_interest_failed")

    def test_zero_next_position_size_is_rejected_before_risk_config(self):
        adapter = self.configured_adapter()
        adapter.sidecar["position"]["value"]["sizeInUsd"] = 0
        adapter.sidecar["pinned_order"]["sizeDeltaUsd"] = 0
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "independent_invalid_next_position_size_usd")
        self.assertEqual(result["model_rule"], "post_update_position_validity")

    def test_reserve_rejection_uses_post_increase_token_oi(self):
        adapter = self.risk_adapter()
        adapter.risk_cells["values"]["reserve_factor_long"] = 0
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "independent_reserve_failed")
        adapter.risk_cells["values"]["reserve_factor_long"] = 10**30
        adapter.risk_cells["values"]["open_interest_reserve_factor_long"] = 0
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "independent_oi_reserve_failed")

    def test_missing_or_zero_index_price_cannot_prove_reserve(self):
        adapter = self.risk_adapter()
        adapter.oracle["prices"][0][1] = 0
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "reserve_oracle_price_unavailable")
        self.assertEqual(result["status"], "gmx_preflight_only")

    def test_pinned_balance_inputs_prove_only_acceptable_price_upper_bound(self):
        adapter = self.balance_adapter()
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["execution_price_upper_bound"], 2630415700000000)
        self.assertLess(result["execution_price_upper_bound"],
                        adapter.sidecar["pinned_order"]["acceptablePrice"])
        self.assertIn("acceptable_price_upper_bound", result["proved_branches"])
        self.assertIn("execution_price_and_acceptable_price", result["missing_rule_evidence"])
        self.assertEqual(result["status"], "gmx_preflight_only")
        self.assertIsNotNone(result["balance_inputs_sha256"])
        self.assertEqual(result["fee_diagnostics"], {
            "ui_fee_amount": 0, "gross_position_fee_amount_before_discounts": 79679})
        self.assertIn("zero_ui_fee_amount", result["proved_branches"])
        self.assertIn("gross_position_fee_before_discounts", result["proved_branches"])

    def test_fee_clocks_expose_elapsed_accrual_without_claiming_net_fees(self):
        adapter = self.balance_adapter(fee_clocks=ROOT / "pinned-fee-clocks.json")
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["fee_diagnostics"], {
            "funding_elapsed_seconds": 51, "borrowing_elapsed_seconds_long": 51,
            "ui_fee_amount": 0, "gross_position_fee_amount_before_discounts": 79679})
        self.assertIn("position_fees_and_collateral_sufficiency",
                      result["missing_rule_evidence"])
        self.assertEqual(result["status"], "gmx_preflight_only")
        self.assertIsNotNone(result["fee_clocks_sha256"])

    def test_smaller_side_switch_proves_zero_long_borrowing_only(self):
        adapter = self.balance_adapter(
            fee_clocks=ROOT / "pinned-fee-clocks.json",
            borrowing_skip=ROOT / "pinned-borrowing-skip.json")
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["fee_diagnostics"]["borrowing_fee_usd"], 0)
        self.assertEqual(result["fee_diagnostics"]["borrowing_fee_amount"], 0)
        self.assertIn("zero_long_borrowing_fee", result["proved_branches"])
        self.assertIn("position_fees_and_collateral_sufficiency",
                      result["missing_rule_evidence"])
        self.assertEqual(result["status"], "gmx_preflight_only")
        adapter.sidecar["position"]["value"]["borrowingFactor"] -= 1
        result = adapter.evaluate(self.preflight(adapter))
        self.assertNotIn("zero_long_borrowing_fee", result["proved_branches"])

    def test_borrowing_skip_key_call_value_and_pin_tampering_rejected(self):
        changes = (
            lambda c: c.update(key="0x" + "0" * 64),
            lambda c: c["rpc_transcript"][2]["params"][0].update(data="0x" + "0" * 72),
            lambda c: c.update(skip_borrowing_fee_for_smaller_side=False),
            lambda c: c["rpc_transcript"][3]["response"]["result"].update(hash="0x" + "0" * 64),
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "pinned borrowing skip"):
                    self.balance_adapter(
                        fee_clocks=ROOT / "pinned-fee-clocks.json",
                        borrowing_skip=self.changed_borrowing_skip(change))

    def test_adaptive_funding_selector_keeps_net_fees_unavailable(self):
        adapter = self.balance_adapter(
            fee_clocks=ROOT / "pinned-fee-clocks.json",
            borrowing_skip=ROOT / "pinned-borrowing-skip.json",
            funding_selector=ROOT / "pinned-funding-selector.json")
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["fee_diagnostics"]["funding_mode"],
                         "adaptive_unreconstructed")
        self.assertNotIn("funding_fee_amount", result["fee_diagnostics"])
        self.assertIn("position_fees_and_collateral_sufficiency",
                      result["missing_rule_evidence"])
        self.assertEqual(result["status"], "gmx_preflight_only")

    def test_funding_selector_tampering_rejected(self):
        changes = (
            lambda c: c.update(key="0x" + "0" * 64),
            lambda c: c["rpc_transcript"][2]["params"][0].update(data="0x" + "0" * 72),
            lambda c: c.update(funding_increase_factor_per_second=0),
            lambda c: c["rpc_transcript"][3]["response"]["result"].update(hash="0x" + "0" * 64),
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "pinned funding selector"):
                    self.balance_adapter(funding_selector=self.changed_funding_selector(change))

    def test_nonzero_snapshotted_ui_fee_does_not_claim_zero_ui_branch(self):
        adapter = self.balance_adapter()
        adapter.sidecar["pinned_order"]["uiFeeFactor"] = 10**28
        result = adapter.evaluate(self.preflight(adapter))
        self.assertNotIn("zero_ui_fee_amount", result["proved_branches"])
        self.assertNotIn("ui_fee_amount", result["fee_diagnostics"])

    def test_fee_clock_key_call_value_and_timestamp_tampering_rejected(self):
        changes = (
            lambda c: c["keys"].update(funding_updated_at="0x" + "0" * 64),
            lambda c: c["rpc_transcript"][2]["params"][0].update(data="0x" + "0" * 72),
            lambda c: c["values"].update(funding_updated_at=0),
            lambda c: c.update(block_timestamp=0),
            lambda c: c["rpc_transcript"][4]["response"]["result"].update(hash="0x" + "0" * 64),
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "pinned fee clocks"):
                    self.balance_adapter(fee_clocks=self.changed_fee_clocks(change))

    def test_price_bound_unavailable_if_balance_not_improved_or_threshold_too_low(self):
        adapter = self.balance_adapter()
        adapter.balance_inputs["values"]["use_oi_tokens_for_balance"] = False
        adapter.balance_inputs["values"]["short_oi_usd_weth_collateral"] = 0
        adapter.balance_inputs["values"]["short_oi_usd_usdc_collateral"] = 0
        result = adapter.evaluate(self.preflight(adapter))
        self.assertNotIn("acceptable_price_upper_bound", result["proved_branches"])
        self.assertIsNone(result["execution_price_upper_bound"])
        adapter = self.balance_adapter()
        adapter.sidecar["pinned_order"]["acceptablePrice"] = 1
        result = adapter.evaluate(self.preflight(adapter))
        self.assertNotIn("acceptable_price_upper_bound", result["proved_branches"])

    def test_balance_inputs_tampering_rejected(self):
        changes = (
            lambda b: b["keys"].update(use_oi_tokens_for_balance="0x" + "0" * 64),
            lambda b: b["rpc_transcript"][2]["params"][0].update(data="0x" + "0" * 72),
            lambda b: b["rpc_transcript"][5]["response"]["result"].update(hash="0x" + "0" * 64),
            lambda b: b["values"].update(use_oi_tokens_for_balance=False),
            lambda b: b["rpc_transcript"][2]["response"].update(result="0x" + "0" * 63 + "2"),
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "pinned balance inputs"):
                    self.balance_adapter(self.changed_balance(change))

    def test_risk_cells_key_call_value_and_block_tampering_rejected(self):
        changes = (
            lambda r: r["keys"].update(max_open_interest_long="0x" + "0" * 64),
            lambda r: r["rpc_transcript"][11]["params"][0].update(data="0x" + "0" * 72),
            lambda r: r["values"].update(min_position_size_usd=0),
            lambda r: r["rpc_transcript"][10]["response"]["result"].update(hash="0x" + "0" * 64),
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "pinned risk cells"):
                    self.risk_adapter(self.changed_risk(change))

    def test_disabled_market_branch_rejects_without_claiming_agreement(self):
        def disable(config):
            config["values"]["is_market_disabled"] = True
            config["rpc_transcript"][3]["response"]["result"] = "0x" + "0" * 63 + "1"
        adapter = self.adapter(config=self.tampered_config(disable))
        result = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(result["reason"], "independent_market_disabled")
        self.assertEqual(result["model_result"], "reject")
        self.assertEqual(result["status"], "gmx_preflight_only")

    def test_config_identity_key_calldata_and_value_tampering_rejected(self):
        changes = (
            lambda c: c.update(block_hash="0x" + "0" * 64),
            lambda c: c["keys"].update(is_market_disabled="0x" + "0" * 64),
            lambda c: c["rpc_transcript"][2]["params"][0].update(data="0x" + "0" * 72),
            lambda c: c["rpc_transcript"][4]["response"]["result"].update(hash="0x" + "0" * 64),
            lambda c: c["values"].update(request_expiration_time=301),
            lambda c: c["rpc_transcript"][3]["response"].update(result="0x" + "0" * 63 + "2"),
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "pinned decision config"):
                    self.adapter(config=self.tampered_config(change))

    def test_config_requires_verified_historical_source(self):
        with self.assertRaisesRegex(ValueError, "requires verified increase executor source"):
            SelectedIncreaseDecisionAdapter(
                ROOT / "long-increase-archive-sidecar.json", ROOT / "sourcify-source-proof.json",
                ROOT / "historical-source-manifest.json", ROOT / "selected-order-creation-oracle.json",
                decision_config_path=ROOT / "pinned-decision-config.json")
        source = json.loads((ROOT / "sourcify-v2/increase_executor.json").read_text())
        source["sources"]["contracts/data/Keys.sol"]["content"] = \
            source["sources"]["contracts/data/Keys.sol"]["content"].replace(
                '"REQUEST_EXPIRATION_TIME"', '"WRONG_EXPIRATION_TIME"', 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, "source or ABI hash mismatch"):
                SelectedIncreaseDecisionAdapter(
                    ROOT / "long-increase-archive-sidecar.json", ROOT / "sourcify-source-proof.json",
                    ROOT / "historical-source-manifest.json", ROOT / "selected-order-creation-oracle.json",
                    ROOT / "pinned-increase-executor.json", path,
                    ROOT / "pinned-swap-handler.json", ROOT / "sourcify-v2/swap_handler.json",
                    ROOT / "pinned-decision-config.json")

    def test_observed_execution_prices_do_not_match_creation_pin(self):
        adapter = self.adapter("selected-order-execution-oracle.json")
        decision = adapter.evaluate(self.preflight(adapter))
        self.assertEqual(decision["reason"], "execution_oracle_and_creation_pin_not_equivalent")

    def test_pin_mismatch_and_digest_tamper_rejected(self):
        adapter = self.adapter()
        preflight = self.preflight(adapter)
        preflight["pin_block_hash_after"] = "0x" + "0" * 64
        self.assertEqual(adapter.evaluate(preflight)["reason"], "selected_order_or_pin_mismatch")
        proof = json.loads((ROOT / "sourcify-source-proof.json").read_text())
        proof["sidecar_sha256"] = "0x" + "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                SelectedIncreaseDecisionAdapter(
                    ROOT / "long-increase-archive-sidecar.json", path,
                    ROOT / "historical-source-manifest.json",
                    ROOT / "selected-order-creation-oracle.json",
                    ROOT / "pinned-increase-executor.json", ROOT / "sourcify-v2/increase_executor.json",
                    ROOT / "pinned-swap-handler.json", ROOT / "sourcify-v2/swap_handler.json")

    def test_independent_supported_branch_rejects_but_full_comparison_stays_closed(self):
        adapter = self.adapter()
        preflight = self.preflight(adapter)
        adapter.sidecar["pinned_order"]["minOutputAmount"] = \
            adapter.sidecar["pinned_order"]["initialCollateralDeltaAmount"] + 1
        preflight["on_chain_request"] = adapter.sidecar["pinned_order"]
        result = adapter.evaluate(preflight)
        self.assertEqual(result["model_rule"], "swap_path_and_min_output")
        self.assertEqual(result["model_result"], "reject")
        self.assertEqual(result["status"], "gmx_preflight_only")

    def test_request_requires_exact_pinned_field_set(self):
        adapter = self.adapter()
        preflight = self.preflight(adapter)
        preflight["on_chain_request"] = dict(preflight["on_chain_request"])
        del preflight["on_chain_request"]["uiFeeFactor"]
        self.assertEqual(adapter.evaluate(preflight)["reason"], "pinned_request_not_equal")
        preflight = self.preflight(adapter)
        preflight["on_chain_request"] = {**preflight["on_chain_request"], "unproved": 1}
        self.assertEqual(adapter.evaluate(preflight)["reason"], "pinned_request_not_equal")

    def test_valid_delegated_proof_at_wrong_pin_rejected(self):
        later = json.loads((ROOT / "execution-oracle-code.json").read_text())
        for role in ("executor", "swap"):
            filename = ("pinned-increase-executor.json" if role == "executor"
                        else "pinned-swap-handler.json")
            proof = json.loads((ROOT / filename).read_text())
            proof["block_number"] = later["block_number"]
            proof["block_hash"] = later["block_hash"]
            for index in (1, 4):
                proof["rpc_transcript"][index]["params"][0] = hex(later["block_number"])
                proof["rpc_transcript"][index]["response"]["result"] = \
                    later["rpc_transcript"][1]["response"]["result"]
            for index in (2, 3):
                proof["rpc_transcript"][index]["params"][1] = hex(later["block_number"])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "wrong-pin.json"
                path.write_text(json.dumps(proof))
                with self.assertRaisesRegex(ValueError, "differs from selected sidecar pin"):
                    SelectedIncreaseDecisionAdapter(
                        ROOT / "long-increase-archive-sidecar.json", ROOT / "sourcify-source-proof.json",
                        ROOT / "historical-source-manifest.json", ROOT / "selected-order-creation-oracle.json",
                        path if role == "executor" else ROOT / "pinned-increase-executor.json",
                        ROOT / "sourcify-v2/increase_executor.json",
                        path if role == "swap" else ROOT / "pinned-swap-handler.json",
                        ROOT / "sourcify-v2/swap_handler.json")


if __name__ == "__main__":
    unittest.main()
