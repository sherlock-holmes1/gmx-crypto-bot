"""Only a verified prestate proof may move a recorded candidate's pin."""

import copy
import json
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.boundary_pin import (
    PIN_SOURCE, pin_candidate_at_prestate_boundary, verified_boundary_proof)


ROOT = Path(__file__).resolve().parents[2] / "evidence/step4-1"
PRESTATE = ROOT / "pinned-system-transaction-prestate.json"
ORACLE = ROOT / "selected-order-execution-oracle.json"
ORDER_KEY = "0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90"
EXECUTION_TX = "0xd7c47b718151b96cc6a25eec97f5f913208d86aa8774c17be4271fb71a27a212"


def candidate(**overrides):
    record = {
        "order_key": ORDER_KEY,
        "proposed_pin_block": 507206345,
        "proposed_pin_hash": "0x" + "ab" * 32,
        "creation": {"block_number": 507206345},
        "terminal": {"block_number": 507206358, "transaction_hash": EXECUTION_TX},
    }
    record.update(overrides)
    return record


class BoundaryPinTests(unittest.TestCase):
    def setUp(self):
        self.oracle = json.loads(ORACLE.read_text())
        self.proof = verified_boundary_proof(PRESTATE, self.oracle, 42161)

    def test_verified_proof_moves_the_pin_to_the_proved_boundary(self):
        rows = [candidate()]
        pinned = pin_candidate_at_prestate_boundary(rows, self.proof, ORDER_KEY)
        self.assertEqual(pinned["proposed_pin_block"], 507206357)
        self.assertEqual(pinned["proposed_pin_hash"],
                         self.proof["equivalent_pin_block_hash"])
        self.assertEqual(pinned["pin_source"], PIN_SOURCE)

    def test_missing_or_unverified_proof_cannot_move_a_pin(self):
        failed = {**self.proof, "prestate_equivalent_to_block_boundary": False}
        for proof in (None, failed):
            with self.subTest(proof=proof is None):
                with self.assertRaises(ValueError):
                    pin_candidate_at_prestate_boundary([candidate()], proof, ORDER_KEY)

    def test_proof_for_another_execution_is_refused(self):
        rows = [candidate(terminal={"block_number": 507206358,
                                    "transaction_hash": "0x" + "cd" * 32})]
        with self.assertRaises(ValueError) as caught:
            pin_candidate_at_prestate_boundary(rows, self.proof, ORDER_KEY)
        self.assertIn("does not name this order's recorded execution", str(caught.exception))

    def test_boundary_outside_the_pending_interval_is_refused(self):
        rows = [candidate(creation={"block_number": 507206358})]
        with self.assertRaises(ValueError) as caught:
            pin_candidate_at_prestate_boundary(rows, self.proof, ORDER_KEY)
        self.assertIn("outside the order's pending interval", str(caught.exception))

    def test_zero_or_many_matching_candidates_are_refused(self):
        for rows in ([], [candidate(), candidate()]):
            with self.subTest(count=len(rows)):
                with self.assertRaises(ValueError):
                    pin_candidate_at_prestate_boundary(rows, self.proof, ORDER_KEY)

    def test_creation_price_evidence_cannot_authorise_a_boundary_pin(self):
        creation = json.loads((ROOT / "selected-order-creation-oracle.json").read_text())
        with self.assertRaises(ValueError) as caught:
            verified_boundary_proof(PRESTATE, creation, 42161)
        self.assertIn("observed-execution oracle evidence", str(caught.exception))

    def test_evidence_naming_two_transactions_is_refused(self):
        evidence = copy.deepcopy(self.oracle)
        evidence["coordinates"][0]["transaction_hash"] = "0x" + "ef" * 32
        with self.assertRaises(ValueError) as caught:
            verified_boundary_proof(PRESTATE, evidence, 42161)
        self.assertIn("not one pinned transaction", str(caught.exception))

    def test_proof_is_rebound_to_the_chain_of_the_caller(self):
        with self.assertRaises(ValueError):
            verified_boundary_proof(PRESTATE, self.oracle, 1)


if __name__ == "__main__":
    unittest.main()
