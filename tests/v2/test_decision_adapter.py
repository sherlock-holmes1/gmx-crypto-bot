import json
import tempfile
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.decision_adapter import SelectedIncreaseDecisionAdapter


ROOT = Path(__file__).resolve().parents[2] / "evidence/step4-1"


class DecisionAdapterTests(unittest.TestCase):
    def adapter(self, oracle="selected-order-creation-oracle.json"):
        return SelectedIncreaseDecisionAdapter(
            ROOT / "long-increase-archive-sidecar.json", ROOT / "sourcify-source-proof.json",
            ROOT / "historical-source-manifest.json", ROOT / oracle,
            ROOT / "pinned-increase-executor.json", ROOT / "sourcify-v2/increase_executor.json",
            ROOT / "pinned-swap-handler.json", ROOT / "sourcify-v2/swap_handler.json")

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
