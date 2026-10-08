import unittest
import json
import tempfile
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.oracle_evidence import (
    capture_recorded_oracle, capture_watched_creation_oracle, verify_historical_oracle_source)
from gmx_crypto_bot_v2.application.router_check import _load_recorded_oracle_evidence


RECORDING = Path(__file__).resolve().parents[2] / "recordings/eth-usdc-v2-sep-20-sep-27"
ROOT = Path(__file__).resolve().parents[2]
TX = "0xac5153f889493d4da056a3f569eb3d0d26f75bb80e757aa6dd05284675b067c0"
WETH = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"


class OracleEvidenceTests(unittest.TestCase):
    def test_selected_creation_transaction(self):
        result = capture_recorded_oracle(RECORDING, TX, (WETH, USDC),
                                         order_transaction_hash=TX,
                                         source_record=ROOT / "evidence/step4-1/sourcify-v2/oracle.json",
                                         pinned_code_proof=ROOT / "evidence/step4-1/pinned-oracle-code.json")
        self.assertEqual(result["prices"][0], [2630415700000000, 2630415700000000])
        self.assertEqual(result["min_timestamp"], 1789932722)
        self.assertEqual(result["max_timestamp"], 1789932722)
        self.assertEqual(result["relationship"],
                         "order_transaction_oracle_before_creation_not_execution_proof")
        self.assertFalse(result["ready_for_observed_execution_comparison"])
        self.assertTrue(result["source_and_scale_verified"])

    def test_missing_and_duplicate_expected_tokens(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            capture_recorded_oracle(RECORDING, TX, (WETH, WETH))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            capture_recorded_oracle(RECORDING, "0x" + "1" * 64, (WETH, USDC))

    def test_pinned_code_transcript_tamper_rejected(self):
        proof = json.loads((ROOT / "evidence/step4-1/pinned-oracle-code.json").read_text())
        proof["rpc_transcript"][2]["response"]["result"] = "0x01"
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / "proof.json"
            altered.write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError, "transcript mismatch"):
                verify_historical_oracle_source(
                    ROOT / "evidence/step4-1/sourcify-v2/oracle.json", altered)
        proof = json.loads((ROOT / "evidence/step4-1/pinned-oracle-code.json").read_text())
        proof["rpc_transcript"][3]["response"]["result"]["number"] = "0x1"
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / "proof.json"
            altered.write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError, "transcript mismatch"):
                verify_historical_oracle_source(
                    ROOT / "evidence/step4-1/sourcify-v2/oracle.json", altered)

    def test_cli_evidence_loader_preserves_counterfactual_and_rejects_tamper(self):
        original = ROOT / "evidence/step4-1/selected-order-creation-oracle.json"
        mapped, evidence = _load_recorded_oracle_evidence(original, RECORDING)
        self.assertEqual(list(mapped), [evidence["order_key"]])
        self.assertFalse(evidence["ready_for_observed_execution_comparison"])
        altered = dict(evidence)
        altered["prices"] = [[1, 1], [2, 2]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "altered.json"
            path.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "calldata mismatch"):
                _load_recorded_oracle_evidence(path, RECORDING)

    def test_watched_creation_oracle_set(self):
        logs = []
        with (RECORDING / "events.jsonl").open() as stream:
            for line in stream:
                row = json.loads(line)
                if row["block_number"] == 507206345 and row.get("payload", {}).get("event_name") in {
                        "OraclePriceUpdate", "OrderCreated"}:
                    logs.append(row["payload"]["log"])
        proof = json.loads((ROOT / "evidence/step4-1/pinned-oracle-code.json").read_text())
        transcript = proof["rpc_transcript"]
        class Rpc:
            def request(self, method, params):
                if method == "eth_chainId":
                    return {"result": hex(42161)}
                if method == "eth_getLogs":
                    return {"result": logs}
                if method == "eth_getCode":
                    return transcript[2]["response"]
                if method == "eth_getBlockByNumber":
                    return transcript[1]["response"]
                raise AssertionError(method)
        candidate = {"order_key": "0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90",
                     "creation": {"block_number": 507206345, "block_hash": proof["block_hash"],
                                  "transaction_hash": TX, "log_index": 18}}
        result = capture_watched_creation_oracle(
            Rpc(), candidate, "0xc8ee91a54287db53897056e12d9819156d3822fb",
            proof["address"], ROOT / "evidence/step4-1/sourcify-v2/oracle.json", (WETH, USDC))
        self.assertTrue(result["source_and_scale_verified"])
        self.assertFalse(result["ready_for_observed_execution_comparison"])
        logs.append(logs[0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            capture_watched_creation_oracle(
                Rpc(), candidate, "0xc8ee91a54287db53897056e12d9819156d3822fb",
                proof["address"], ROOT / "evidence/step4-1/sourcify-v2/oracle.json", (WETH, USDC))
        logs.pop()
        original_header = transcript[1]["response"]
        transcript[1]["response"] = {"result": {**original_header["result"], "number": "0x1"}}
        with self.assertRaisesRegex(ValueError, "block hash mismatch"):
            capture_watched_creation_oracle(
                Rpc(), candidate, "0xc8ee91a54287db53897056e12d9819156d3822fb",
                proof["address"], ROOT / "evidence/step4-1/sourcify-v2/oracle.json", (WETH, USDC))

    def test_execution_price_age_and_chain_transcript(self):
        evidence = ROOT / "evidence/step4-1/selected-order-execution-oracle.json"
        mapped, row = _load_recorded_oracle_evidence(evidence, RECORDING)
        self.assertEqual(list(mapped), [row["order_key"]])
        self.assertEqual(row["max_price_age_seconds_at_pin"], 1)
        self.assertEqual(row["max_oracle_price_age_seconds"], 300)
        self.assertTrue(row["age_within_historical_max"])
        self.assertFalse(row["ready_for_observed_execution_comparison"])
        proof = json.loads((ROOT / "evidence/step4-1/execution-oracle-code.json").read_text())
        proof["rpc_transcript"][0]["response"]["result"] = "0x1"
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / "proof.json"
            altered.write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError, "transcript mismatch"):
                verify_historical_oracle_source(
                    ROOT / "evidence/step4-1/sourcify-v2/oracle.json", altered)


if __name__ == "__main__":
    unittest.main()
