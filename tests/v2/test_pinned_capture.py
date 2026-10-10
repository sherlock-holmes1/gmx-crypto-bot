"""The pinned decision transcripts must be captured by tooling, not by hand.

Each capture is replayed through the adapter's own verifier: the mock serves the
saved boundary-pin responses, the capture rebuilds the record, and the verifier
must rebuild every key and value from the result. Failure paths must refuse a
record rather than save a weaker one.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck import pinned_capture as capture
from gmx_crypto_bot_v2.crosscheck.decision_adapter import (
    verify_pinned_balance_inputs, verify_pinned_borrowing_skip, verify_pinned_decision_config,
    verify_pinned_fee_clocks, verify_pinned_funding_selector, verify_pinned_increase_executor,
    verify_pinned_risk_cells, verify_pinned_swap_handler,
)


ROOT = Path(__file__).resolve().parents[2] / "evidence/step4-1"
SIDECAR = json.loads((ROOT / "long-increase-archive-sidecar-507206357.json").read_text())
EXECUTOR_SOURCE = ROOT / "sourcify-v2/increase_executor.json"
SWAP_SOURCE = ROOT / "sourcify-v2/swap_handler.json"
BLOCK = 507206357
BLOCK_HASH = "0x6624873b47717258e06e038aa89bf86c2bd9b34542b5b3f72b5b04badc8b4af5"
CHAIN_ID = 42161
DATASTORE = SIDECAR["deployment"]["datastore"]
ORDER_HANDLER = SIDECAR["deployment"]["order_handler"]
MARKET = SIDECAR["pinned_order"]["market"]
TOKENS = SIDECAR["fixed_values"]["market"]


class ReplayRpc:
    """Serve the saved boundary responses for exactly the calls they recorded."""

    def __init__(self, *records, errors=(), chain_id=None, drift=None):
        self.responses = {}
        for record in records:
            for row in record["rpc_transcript"]:
                key = (row["method"], json.dumps(row["params"], sort_keys=True))
                self.responses[key] = row["response"]
        self.errors = set(errors)
        self.chain_id = chain_id
        self.drift = drift
        self.header_calls = 0

    def request(self, method, params):
        if method in self.errors:
            return {"error": {"message": "https://secret@provider/key is unavailable"}}
        if method == "eth_chainId" and self.chain_id is not None:
            return {"jsonrpc": "2.0", "id": 1, "result": self.chain_id}
        if method == "eth_getBlockByNumber":
            self.header_calls += 1
            if self.drift is not None and self.header_calls > 1:
                return {"jsonrpc": "2.0", "id": 1,
                        "result": {"number": hex(BLOCK), "hash": self.drift,
                                   "timestamp": "0x6ab034b5"}}
        key = (method, json.dumps(params, sort_keys=True))
        if key not in self.responses:
            raise AssertionError("unexpected call " + str(key))
        return self.responses[key]


def saved(name):
    return json.loads((ROOT / f"pinned-507206357-{name}.json").read_text())


class PinnedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pinned = {"chain_id": CHAIN_ID, "block": BLOCK, "block_hash": BLOCK_HASH}
        self.cells = {**self.pinned, "datastore": DATASTORE}

    def write(self, record, name="record.json"):
        path = Path(self.directory.name) / name
        path.write_text(json.dumps(record, indent=2, sort_keys=True))
        return path

    def plans(self):
        """Every record, with its capture call and the verifier that replays it."""
        return {
            "decision-config": (
                lambda rpc: capture.capture_decision_config(rpc, market=MARKET, **self.cells),
                lambda path: verify_pinned_decision_config(path, EXECUTOR_SOURCE, SIDECAR)),
            "risk-cells": (
                lambda rpc: capture.capture_risk_cells(
                    rpc, market_token=TOKENS["MARKET_TOKEN"], long_token=TOKENS["LONG_TOKEN"],
                    short_token=TOKENS["SHORT_TOKEN"], **self.cells),
                lambda path: verify_pinned_risk_cells(path, EXECUTOR_SOURCE, SIDECAR)),
            "balance-inputs": (
                lambda rpc: capture.capture_balance_inputs(
                    rpc, market_token=TOKENS["MARKET_TOKEN"], long_token=TOKENS["LONG_TOKEN"],
                    short_token=TOKENS["SHORT_TOKEN"], **self.cells),
                lambda path: verify_pinned_balance_inputs(path, EXECUTOR_SOURCE, SIDECAR)),
            "fee-clocks": (
                lambda rpc: capture.capture_fee_clocks(rpc, market=MARKET, **self.cells),
                lambda path: verify_pinned_fee_clocks(path, EXECUTOR_SOURCE, SIDECAR)),
            "borrowing-skip": (
                lambda rpc: capture.capture_borrowing_skip(rpc, **self.cells),
                lambda path: verify_pinned_borrowing_skip(path, EXECUTOR_SOURCE, SIDECAR)),
            "funding-selector": (
                lambda rpc: capture.capture_funding_selector(rpc, market=MARKET, **self.cells),
                lambda path: verify_pinned_funding_selector(path, EXECUTOR_SOURCE, SIDECAR)),
            "increase-executor": (
                lambda rpc: capture.capture_increase_executor(
                    rpc, order_handler=ORDER_HANDLER, **self.pinned),
                lambda path: verify_pinned_increase_executor(path, EXECUTOR_SOURCE)),
            "swap-handler": (
                lambda rpc: capture.capture_swap_handler(
                    rpc, order_handler=ORDER_HANDLER, **self.pinned),
                lambda path: verify_pinned_swap_handler(path, SWAP_SOURCE)),
        }

    def test_every_record_is_recaptured_and_replayed_by_its_own_verifier(self):
        for name, (build, check) in self.plans().items():
            with self.subTest(record=name):
                record = saved(name)
                rebuilt = build(ReplayRpc(record))
                self.assertEqual({k: v for k, v in rebuilt.items() if k != "rpc_transcript"},
                                 {k: v for k, v in record.items() if k != "rpc_transcript"})
                self.assertEqual(rebuilt["block_number"], BLOCK)
                self.assertEqual(rebuilt["block_hash"], BLOCK_HASH)
                check(self.write(rebuilt, name + ".json"))

    def test_captures_never_record_an_endpoint(self):
        for name in self.plans():
            with self.subTest(record=name):
                self.assertTrue(capture.endpoint_free(saved(name)))

    def test_a_provider_error_body_is_redacted_and_fails_the_capture(self):
        traced = capture.TranscriptRpc(ReplayRpc(saved("borrowing-skip"),
                                                 errors={"eth_chainId"}))
        with self.assertRaises(OSError):
            traced.request("eth_chainId", [])
        self.assertEqual(traced.calls[-1]["response"], {"error": "redacted_provider_error"})
        self.assertTrue(capture.endpoint_free({"rpc_transcript": traced.calls}))

    def test_a_write_method_is_refused(self):
        traced = capture.TranscriptRpc(ReplayRpc(saved("borrowing-skip")))
        with self.assertRaises(ValueError):
            traced.request("eth_sendRawTransaction", ["0x00"])
        self.assertEqual(traced.calls, [])

    def test_a_chain_mismatch_fails_the_capture(self):
        rpc = ReplayRpc(saved("funding-selector"), chain_id="0x1")
        with self.assertRaises(ValueError) as caught:
            capture.capture_funding_selector(rpc, market=MARKET, **self.cells)
        self.assertIn("chain mismatch", str(caught.exception))

    def test_a_block_that_changes_during_the_capture_fails_closed(self):
        rpc = ReplayRpc(saved("funding-selector"), drift="0x" + "99" * 32)
        with self.assertRaises(ValueError) as caught:
            capture.capture_funding_selector(rpc, market=MARKET, **self.cells)
        self.assertIn("block identity mismatch", str(caught.exception))

    def test_a_different_expected_block_hash_fails_the_capture(self):
        rpc = ReplayRpc(saved("funding-selector"))
        with self.assertRaises(ValueError):
            capture.capture_funding_selector(
                rpc, market=MARKET, datastore=DATASTORE, chain_id=CHAIN_ID,
                block=BLOCK, block_hash="0x" + "44" * 32)

    def test_a_malformed_boolean_cell_is_refused(self):
        record = saved("borrowing-skip")
        for row in record["rpc_transcript"]:
            if row["method"] == "eth_call":
                row["response"] = {"jsonrpc": "2.0", "id": 1, "result": "0x" + "00" * 31 + "02"}
        with self.assertRaises(ValueError) as caught:
            capture.capture_borrowing_skip(ReplayRpc(record), **self.cells)
        self.assertIn("malformed boolean", str(caught.exception))

    def test_a_fee_clock_later_than_the_block_is_refused(self):
        record = saved("fee-clocks")
        calls = [row for row in record["rpc_transcript"] if row["method"] == "eth_call"]
        calls[0]["response"] = {"jsonrpc": "2.0", "id": 1,
                                "result": "0x" + (record["block_timestamp"] + 1).to_bytes(32, "big").hex()}
        with self.assertRaises(ValueError) as caught:
            capture.capture_fee_clocks(ReplayRpc(record), market=MARKET, **self.cells)
        self.assertIn("later than the pinned block timestamp", str(caught.exception))

    def test_a_contract_without_code_at_the_pin_is_refused(self):
        record = saved("swap-handler")
        for row in record["rpc_transcript"]:
            if row["method"] == "eth_getCode":
                row["response"] = {"jsonrpc": "2.0", "id": 1, "result": "0x"}
        with self.assertRaises(ValueError) as caught:
            capture.capture_swap_handler(ReplayRpc(record), order_handler=ORDER_HANDLER,
                                         **self.pinned)
        self.assertIn("no code at the pin", str(caught.exception))

    def test_captured_keys_are_derived_locally_not_copied(self):
        # A capture that trusted a supplied slot would pass its own verifier
        # only because both read the same wrong cell; these keys come from the
        # historical Keys.sol formulas the verifier recomputes independently.
        record = capture.capture_risk_cells(
            ReplayRpc(saved("risk-cells")), market_token=TOKENS["MARKET_TOKEN"],
            long_token=TOKENS["LONG_TOKEN"], short_token=TOKENS["SHORT_TOKEN"], **self.cells)
        self.assertEqual(len(record["keys"]), 9)
        self.assertEqual(len(set(record["keys"].values())), 9)
        self.assertEqual(set(record["keys"]), set(record["values"]))


if __name__ == "__main__":
    unittest.main()
