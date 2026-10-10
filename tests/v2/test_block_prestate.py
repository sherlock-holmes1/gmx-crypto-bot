import json
import tempfile
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.block_prestate import (
    DOES_NOT_PROVE, PROVES, PrestateRpc,
    capture_system_transaction_prestate, verify_system_transaction_prestate,
)
from prestate_helper import (
    BLOCK, BLOCK_HASH, BOUNDARY_HASH, EXECUTION_TX, GMX_ORDER_HANDLER, SYSTEM_TX,
    TIMESTAMP, MockRpc, capture, system_receipt, system_transaction,
)


ROOT = Path(__file__).resolve().parents[2] / "evidence/step4-1"
REAL_PROOF = ROOT / "pinned-system-transaction-prestate.json"


class BlockPrestateCaptureTests(unittest.TestCase):
    def test_single_arbos_start_block_transaction_proves_block_boundary(self):
        record = capture(MockRpc())
        self.assertTrue(record["prestate_equivalent_to_block_boundary"])
        self.assertEqual(record["reason"],
                         "all_preceding_transactions_are_arbos_internal_start_block_transactions")
        self.assertEqual(record["equivalent_pin_block"], BLOCK - 1)
        self.assertEqual(record["observed_transaction_hash"], EXECUTION_TX)
        self.assertEqual([row["method"] for row in record["rpc_transcript"]], [
            "eth_chainId", "eth_getBlockByNumber", "eth_getBlockByNumber",
            "eth_getTransactionByBlockNumberAndIndex", "eth_getTransactionReceipt",
            "eth_getBlockByNumber"])
        self.assertEqual(record["equivalent_pin_block_hash"], BOUNDARY_HASH)
        self.assertEqual(record["execution_block_timestamp"], 1789932725)
        self.assertEqual(record["equivalent_pin_block_timestamp"], 1789932725)
        # The ArbOS L1 view is exactly what the proved transaction advances.
        self.assertNotEqual(record["arbos_l1_block_number"]["execution_block"],
                            record["arbos_l1_block_number"]["equivalent_pin_block"])
        entry = record["preceding_transactions"][0]
        self.assertEqual(entry["classification"], "arbos_internal_start_block_transaction")
        self.assertEqual((entry["gas"], entry["gas_price"], entry["value"]), (0, 0, 0))
        self.assertEqual((entry["receipt_status"], entry["receipt_gas_used"],
                          entry["receipt_log_count"]), (1, 0, 0))
        self.assertEqual(record["proves"], PROVES)
        self.assertEqual(record["does_not_prove"], list(DOES_NOT_PROVE))

    def test_transaction_index_zero_has_no_preceding_transaction(self):
        rpc = MockRpc()
        record = capture(rpc, index=0, expected_transaction_hash=SYSTEM_TX)
        self.assertTrue(record["prestate_equivalent_to_block_boundary"])
        self.assertEqual(record["reason"], "no_preceding_transactions_in_block")
        self.assertEqual(record["preceding_transactions"], [])
        self.assertNotIn("eth_getTransactionReceipt", [m for m, _ in rpc.requests])

    def test_every_failure_path_fails_closed_with_an_exact_reason(self):
        cases = {
            "chain_id_mismatch": MockRpc(chain_id="0x1"),
            "chain_id_unavailable": MockRpc(errors=["eth_chainId"]),
            "block_header_unavailable_before": MockRpc(errors=["eth_getBlockByNumber"]),
            "reorg_detected": MockRpc(header_after={
                "number": hex(BLOCK), "hash": "0x" + "1" * 64,
                "transactions": [SYSTEM_TX, EXECUTION_TX]}),
            "observed_transaction_index_out_of_range": MockRpc(header={
                "number": hex(BLOCK), "hash": BLOCK_HASH, "parentHash": BOUNDARY_HASH,
                "timestamp": TIMESTAMP, "transactions": [SYSTEM_TX]}),
            "execution_block_timestamp_unavailable": MockRpc(header={
                "number": hex(BLOCK), "hash": BLOCK_HASH, "parentHash": BOUNDARY_HASH,
                "transactions": [SYSTEM_TX, EXECUTION_TX]}),
            "execution_block_parent_hash_malformed": MockRpc(header={
                "number": hex(BLOCK), "hash": BLOCK_HASH, "timestamp": TIMESTAMP,
                "transactions": [SYSTEM_TX, EXECUTION_TX]}),
            "block_header_number_mismatch_equivalent_pin": MockRpc(boundary=None),
            "equivalent_pin_block_hash_mismatch": MockRpc(boundary={
                "number": hex(BLOCK - 1), "hash": "0x" + "8" * 64,
                "timestamp": TIMESTAMP, "transactions": []}),
            "block_timestamp_advances_across_pin_boundary": MockRpc(boundary={
                "number": hex(BLOCK - 1), "hash": BOUNDARY_HASH,
                "timestamp": "0x6ab034b4", "transactions": []}),
            "preceding_transaction_unavailable": MockRpc(transactions={}),
            "preceding_transaction_is_not_arbos_internal_account": MockRpc(
                transactions={0: system_transaction(to=GMX_ORDER_HANDLER)}),
            "preceding_transaction_is_not_arbos_internal_type": MockRpc(
                transactions={0: system_transaction(type="0x2")}),
            "preceding_transaction_reserves_gas": MockRpc(
                transactions={0: system_transaction(gas="0x5208")}),
            "preceding_transaction_transfers_value": MockRpc(
                transactions={0: system_transaction(value="0x1")}),
            "preceding_transaction_is_not_arbos_start_block_call": MockRpc(
                transactions={0: system_transaction(input="0xa9059cbb" + "00" * 64)}),
            "preceding_transaction_block_hash_mismatch": MockRpc(
                transactions={0: system_transaction(blockHash="0x" + "2" * 64)}),
            "preceding_receipt_unavailable": MockRpc(receipts={}),
            "preceding_receipt_consumed_gas": MockRpc(
                receipts={SYSTEM_TX: system_receipt(gasUsed="0x1")}),
            "preceding_receipt_emitted_logs": MockRpc(
                receipts={SYSTEM_TX: system_receipt(logs=[{"address": GMX_ORDER_HANDLER}])}),
            "preceding_receipt_status_not_successful": MockRpc(
                receipts={SYSTEM_TX: system_receipt(status="0x0")}),
            "preceding_receipt_created_contract": MockRpc(
                receipts={SYSTEM_TX: system_receipt(contractAddress=GMX_ORDER_HANDLER)}),
            "preceding_receipt_target_is_not_arbos_internal_account": MockRpc(
                receipts={SYSTEM_TX: system_receipt(to=GMX_ORDER_HANDLER)}),
        }
        for reason, rpc in cases.items():
            with self.subTest(reason=reason):
                record = capture(rpc)
                self.assertFalse(record["prestate_equivalent_to_block_boundary"])
                self.assertEqual(record["reason"], reason)
                self.assertEqual(record["equivalent_pin_block"], BLOCK - 1)

    def test_observed_transaction_hash_must_match_the_recorded_execution(self):
        record = capture(MockRpc(), expected_transaction_hash="0x" + "9" * 64)
        self.assertFalse(record["prestate_equivalent_to_block_boundary"])
        self.assertEqual(record["reason"], "observed_transaction_hash_mismatch")

    def test_provider_error_bodies_are_redacted_from_the_transcript(self):
        record = capture(MockRpc(errors=["eth_chainId"]))
        self.assertEqual(record["rpc_transcript"][0]["response"],
                         {"error": "redacted_provider_error"})
        self.assertNotIn("secret", json.dumps(record))

    def test_capture_rejects_invalid_coordinates_and_write_methods(self):
        for kwargs in ({"block_number": 0}, {"transaction_index": -1},
                       {"block_hash": "0x01"}, {"chain_id": 0}):
            with self.subTest(kwargs=kwargs):
                arguments = {"chain_id": 42161, "block_number": BLOCK,
                             "block_hash": BLOCK_HASH, "transaction_index": 1, **kwargs}
                with self.assertRaisesRegex(ValueError, "invalid observed execution coordinate"):
                    capture_system_transaction_prestate(MockRpc(), **arguments)
        with self.assertRaisesRegex(ValueError, "read-only methods only"):
            PrestateRpc(MockRpc()).request("eth_sendRawTransaction", [])
        with self.assertRaisesRegex(ValueError, "read-only methods only"):
            PrestateRpc(MockRpc()).request("eth_call", [])


class BlockPrestateVerifyTests(unittest.TestCase):
    def saved(self, record):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "prestate.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True))
        return path

    def verify(self, path, **overrides):
        arguments = {"chain_id": 42161, "execution_block_number": BLOCK,
                     "execution_block_hash": BLOCK_HASH, "observed_transaction_index": 1,
                     "observed_transaction_hash": EXECUTION_TX, **overrides}
        return verify_system_transaction_prestate(path, **arguments)

    def test_captured_proof_round_trips_through_verification(self):
        record = self.verify(self.saved(capture(MockRpc())))
        self.assertTrue(record["prestate_equivalent_to_block_boundary"])
        self.assertEqual(record["preceding_transactions"][0]["transaction_hash"], SYSTEM_TX)

    def test_failed_proof_is_returned_unchanged_rather_than_raising(self):
        record = self.verify(self.saved(capture(MockRpc(receipts={}))))
        self.assertFalse(record["prestate_equivalent_to_block_boundary"])
        self.assertEqual(record["reason"], "preceding_receipt_unavailable")

    def test_identity_binding_rejects_another_coordinate(self):
        path = self.saved(capture(MockRpc()))
        for overrides in ({"chain_id": 1}, {"execution_block_number": BLOCK + 1},
                          {"execution_block_hash": "0x" + "3" * 64},
                          {"observed_transaction_index": 2}):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    self.verify(path, **overrides)
        with self.assertRaisesRegex(ValueError, "observed transaction mismatch"):
            self.verify(path, observed_transaction_hash="0x" + "4" * 64)

    def test_claimed_equivalence_must_be_rebuilt_from_the_raw_transcript(self):
        def tampered(change, redigest=True):
            record = capture(MockRpc())
            change(record)
            if redigest:
                from gmx_crypto_bot_v2.crosscheck import block_prestate
                record["rpc_transcript_sha256"] = block_prestate._transcript_digest(
                    record["rpc_transcript"])
            return self.saved(record)

        changes = {
            "transcript digest mismatch": (
                lambda r: r["rpc_transcript"][4]["response"]["result"].update(gasUsed="0x1"),
                False),
            "preceding_receipt_consumed_gas": (
                lambda r: r["rpc_transcript"][4]["response"]["result"].update(gasUsed="0x1"), True),
            "preceding_receipt_emitted_logs": (
                lambda r: r["rpc_transcript"][4]["response"]["result"].update(
                    logs=[{"address": GMX_ORDER_HANDLER}]), True),
            "preceding_transaction_is_not_arbos_internal_account": (
                lambda r: r["rpc_transcript"][3]["response"]["result"].update(
                    to=GMX_ORDER_HANDLER), True),
            "reorg_detected": (
                lambda r: r["rpc_transcript"][5]["response"]["result"].update(
                    hash="0x" + "5" * 64), True),
            "equivalent_pin_block_hash_mismatch": (
                lambda r: r["rpc_transcript"][2]["response"]["result"].update(
                    hash="0x" + "6" * 64), True),
            "block_timestamp_advances_across_pin_boundary": (
                lambda r: r["rpc_transcript"][2]["response"]["result"].update(
                    timestamp="0x6ab034b4"), True),
            "equivalent_pin_block_record_mismatch": (
                lambda r: r.update(equivalent_pin_block_hash="0x" + "a" * 64), True),
            "verdict_reason_mismatch": (lambda r: r.update(reason="fabricated"), True),
            "transcript shape mismatch": (lambda r: r["rpc_transcript"].pop(), True),
        }
        for expected, (change, redigest) in changes.items():
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    self.verify(tampered(change, redigest))

    def test_rewritten_summary_cannot_outrun_the_transcript(self):
        record = capture(MockRpc())
        record["preceding_transactions"][0]["receipt_gas_used"] = 21000
        with self.assertRaisesRegex(ValueError, "preceding_transaction_record_mismatch"):
            self.verify(self.saved(record))
        record = capture(MockRpc(receipts={}))
        record["prestate_equivalent_to_block_boundary"] = True
        with self.assertRaisesRegex(ValueError, "transcript shape mismatch"):
            self.verify(self.saved(record))

    def test_honesty_text_is_part_of_the_bound_identity(self):
        record = capture(MockRpc())
        record["does_not_prove"] = []
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.verify(self.saved(record))

    def test_boundary_hash_is_derived_from_the_execution_header_parent(self):
        """The pin block hash is never trusted from the summary; it is re-derived."""
        record = capture(MockRpc())
        self.assertEqual(record["rpc_transcript"][1]["response"]["result"]["parentHash"],
                         record["equivalent_pin_block_hash"])
        verified = self.verify(self.saved(record))
        self.assertEqual(verified["equivalent_pin_block_hash"], BOUNDARY_HASH)

    def test_real_captured_arbitrum_transcript_verifies(self):
        """The saved evidence file is a real public-endpoint capture, not a fixture."""
        record = verify_system_transaction_prestate(
            REAL_PROOF, chain_id=42161, execution_block_number=BLOCK,
            execution_block_hash=BLOCK_HASH, observed_transaction_index=1,
            observed_transaction_hash=EXECUTION_TX)
        self.assertTrue(record["prestate_equivalent_to_block_boundary"])
        self.assertEqual(record["equivalent_pin_block"], 507206357)
        self.assertEqual(len(record["preceding_transactions"]), 1)
        self.assertEqual(record["preceding_transactions"][0]["transaction_hash"], SYSTEM_TX)
        self.assertEqual(record["endpoint"], "https://arb1.arbitrum.io/rpc")
        self.assertEqual(record["equivalent_pin_block_hash"], BOUNDARY_HASH)
        self.assertEqual(record["execution_block_timestamp"],
                         record["equivalent_pin_block_timestamp"])
        self.assertEqual(record["arbos_l1_block_number"],
                         {"execution_block": 26020836, "equivalent_pin_block": 26020834})


class RouterCheckPrestateArgumentTests(unittest.TestCase):
    def cli(self, *arguments):
        from gmx_crypto_bot_v2.application import router_check
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as raised:
                router_check.main(["--recording", directory,
                                   "--output", str(Path(directory) / "report.json"),
                                   *arguments])
        return raised.exception.code

    def test_prestate_arguments_require_the_full_decision_gate(self):
        self.assertEqual(self.cli("--prestate-proof", str(REAL_PROOF)), 2)
        self.assertEqual(self.cli("--capture-prestate-proof", str(REAL_PROOF)), 2)
        self.assertEqual(self.cli("--prestate-proof", str(REAL_PROOF),
                                  "--capture-prestate-proof", str(REAL_PROOF)), 2)

    def test_capture_refuses_oracle_evidence_that_is_not_an_observed_execution(self):
        from gmx_crypto_bot_v2.application.router_check import _capture_prestate_proof
        creation = json.loads((ROOT / "selected-order-creation-oracle.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "observed-execution oracle evidence"):
                _capture_prestate_proof(Path(directory) / "out.json", creation, 42161,
                                        "https://invalid.example/rpc")

    def test_boundary_pin_flag_requires_a_prestate_proof(self):
        self.assertEqual(self.cli("--pin-at-prestate-boundary"), 2)

    def test_boundary_pin_only_accepts_a_verified_proof_and_the_matching_order(self):
        from gmx_crypto_bot_v2.application.router_check import (
            _pin_candidate_at_prestate_boundary)

        class Adapter:
            def __init__(self, prestate):
                self.prestate = prestate
                self.oracle = {"order_key": "0x" + "ab" * 32}

        proof = capture(MockRpc())
        key = "0x" + "ab" * 32
        candidate = {"order_key": key,
                     "creation": {"block_number": BLOCK - 13},
                     "terminal": {"block_number": BLOCK, "transaction_hash": EXECUTION_TX}}
        pinned = _pin_candidate_at_prestate_boundary([candidate], Adapter(proof))
        self.assertEqual(pinned["proposed_pin_block"], BLOCK - 1)
        self.assertEqual(pinned["proposed_pin_hash"], BOUNDARY_HASH)
        self.assertEqual(pinned["pin_source"], "prestate_proved_pre_execution_boundary")
        with self.assertRaisesRegex(ValueError, "verified prestate proof"):
            _pin_candidate_at_prestate_boundary(
                [dict(candidate)], Adapter(capture(MockRpc(receipts={}))))
        with self.assertRaisesRegex(ValueError, "exactly one matching candidate"):
            _pin_candidate_at_prestate_boundary([], Adapter(proof))
        other = {**candidate, "terminal": {"block_number": BLOCK,
                                           "transaction_hash": "0x" + "cd" * 32}}
        with self.assertRaisesRegex(ValueError, "recorded execution"):
            _pin_candidate_at_prestate_boundary([other], Adapter(proof))
        late = {**candidate, "creation": {"block_number": BLOCK}}
        with self.assertRaisesRegex(ValueError, "pending interval"):
            _pin_candidate_at_prestate_boundary([late], Adapter(proof))

    def test_non_json_provider_body_degrades_to_a_redacted_error(self):
        import io
        from unittest import mock
        from gmx_crypto_bot_v2.application import router_check

        class Body(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with mock.patch.object(router_check.urllib.request, "urlopen",
                               return_value=Body(b"<html>rate limited</html>")):
            result = router_check.HttpRpc("https://invalid.example/rpc").request(
                "eth_chainId", [])
        self.assertEqual(result, {"error": {"message": "JSONDecodeError"}})

    def test_http_client_still_refuses_write_methods(self):
        from gmx_crypto_bot_v2.application.router_check import HttpRpc
        with self.assertRaisesRegex(ValueError, "read-only RPC method required"):
            HttpRpc("https://invalid.example/rpc").request("eth_sendRawTransaction", ["0x00"])


if __name__ == "__main__":
    unittest.main()
