"""The archive sidecar must be re-derivable from its own raw transcript.

`fixed_values.market` and `position.value` feed reject-side rules, and a
reject-side `disagreement` is a claim that GMX and our model differ at
equivalent state. These tests require every reported sidecar value to come back
out of the saved calls, and require any tampering to fail closed.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.decision_adapter import SelectedIncreaseDecisionAdapter
from gmx_crypto_bot_v2.crosscheck.sidecar_replay import verify_sidecar_transcript_values


ROOT = Path(__file__).resolve().parents[2] / "evidence/step4-1"
CREATION_PIN_SIDECAR = ROOT / "long-increase-archive-sidecar.json"
BOUNDARY_PIN_SIDECAR = ROOT / "long-increase-archive-sidecar-507206357.json"


def _load(path):
    return json.loads(path.read_text())


class SidecarReplayTests(unittest.TestCase):
    def setUp(self):
        self.sidecar = _load(BOUNDARY_PIN_SIDECAR)

    def tampered(self, change):
        record = copy.deepcopy(self.sidecar)
        change(record)
        return record

    def refuses(self, change, fragment):
        with self.assertRaises(ValueError) as caught:
            verify_sidecar_transcript_values(self.tampered(change))
        self.assertIn(fragment, str(caught.exception))

    def test_both_saved_sidecars_replay_from_their_own_transcripts(self):
        for path, block in ((CREATION_PIN_SIDECAR, 507206345),
                            (BOUNDARY_PIN_SIDECAR, 507206357)):
            with self.subTest(sidecar=path.name):
                replay = verify_sidecar_transcript_values(_load(path))
                self.assertEqual(replay["pin_block"], block)
                self.assertTrue(replay["sidecar_values_rederived_from_raw_transcript"])
                self.assertEqual(replay["market"]["INDEX_TOKEN"],
                                 replay["market"]["LONG_TOKEN"])

    def test_replay_reports_the_transcript_digest_and_derived_position_key(self):
        replay = verify_sidecar_transcript_values(self.sidecar)
        self.assertEqual(replay["rpc_transcript_sha256"],
                         self.sidecar["rpc_transcript_sha256"])
        self.assertEqual(replay["position_key"], self.sidecar["position"]["key"])
        self.assertEqual(replay["order_key"], self.sidecar["source"]["order_key"])

    def test_changed_market_identity_is_refused(self):
        # A silently swapped collateral token would decide
        # market_and_collateral_token_valid without any chain evidence.
        self.refuses(
            lambda record: record["fixed_values"]["market"].update(
                SHORT_TOKEN="0x" + "11" * 20),
            "fixed values differ from its transcript")

    def test_changed_position_size_is_refused(self):
        self.refuses(lambda record: record["position"]["value"].update(sizeInUsd=1),
                     "position differs from its transcript")

    def test_changed_position_key_is_refused(self):
        self.refuses(lambda record: record["position"].update(key="0x" + "00" * 32),
                     "position differs from its transcript")

    def test_changed_order_field_is_refused(self):
        self.refuses(lambda record: record["pinned_order"].update(sizeDeltaUsd=1),
                     "pinned order differs from its transcript")

    def test_changed_fixed_value_is_refused(self):
        self.refuses(
            lambda record: record["fixed_values"]["liquidity"].update(
                **{"pool:0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 1}),
            "fixed values differ from its transcript")

    def test_changed_open_interest_aggregate_is_refused(self):
        self.refuses(
            lambda record: record["virtual_inventory"]["open_interest_tokens"].update(long=1),
            "virtual inventory differs from its transcript")

    def test_changed_router_gate_is_refused(self):
        self.refuses(lambda record: record["router_gate"].update(nonce=1),
                     "router gate differs from its transcript")

    def test_changed_feature_flag_is_refused(self):
        self.refuses(lambda record: record["feature_flag"].update(disabled=True),
                     "feature flag differs from its transcript")

    def test_changed_fixed_cell_inventory_is_refused(self):
        self.refuses(lambda record: record["fixed_cells"].pop(),
                     "fixed cell inventory differs")

    def test_rewritten_call_result_without_a_new_digest_is_refused(self):
        def rewrite(record):
            for row in record["rpc_transcript"]:
                if row["method"] == "eth_call":
                    row["response"]["result"] = "0x" + "00" * 32
                    return
        self.refuses(rewrite, "transcript digest mismatch")

    def test_rewritten_call_result_with_a_matching_digest_is_still_refused(self):
        import hashlib

        def rewrite(record):
            for row in record["rpc_transcript"]:
                if row["method"] == "eth_call":
                    row["response"]["result"] = "0x" + "00" * 32
            record["rpc_transcript_sha256"] = "0x" + hashlib.sha256(json.dumps(
                record["rpc_transcript"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
        # Recomputing the digest hides the edit from the digest check, but the
        # decoded value no longer matches what the sidecar reports.
        with self.assertRaises(ValueError):
            verify_sidecar_transcript_values(self.tampered(rewrite))

    def test_changed_pin_hash_is_refused(self):
        self.refuses(lambda record: record["pin"].update(recorded_hash="0x" + "22" * 32),
                     "pin hashes disagree")

    def test_a_call_at_another_block_is_refused(self):
        def repin(record):
            record["pin"]["number"] = 507206345
            record["pin"]["recorded_hash"] = record["pin"]["archive_hash"]
        self.refuses(repin, "transcript block mismatch")

    def test_a_dropped_call_is_refused(self):
        def drop(record):
            record["rpc_transcript"] = [row for row in record["rpc_transcript"]
                                        if row["method"] != "eth_call"]
        self.refuses(drop, "transcript digest mismatch")

    def test_two_results_for_one_call_are_refused(self):
        def duplicate(record):
            row = next(r for r in record["rpc_transcript"] if r["method"] == "eth_call")
            clone = copy.deepcopy(row)
            clone["response"]["result"] = "0x" + "00" * 32
            record["rpc_transcript"].append(clone)
            import hashlib
            record["rpc_transcript_sha256"] = "0x" + hashlib.sha256(json.dumps(
                record["rpc_transcript"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
        self.refuses(duplicate, "repeats a call with two results")

    def test_an_uncollected_sidecar_is_refused(self):
        self.refuses(lambda record: record.update(status="ineligible_latest_request"),
                     "requires a collected archive snapshot")

    def test_adapter_refuses_a_sidecar_it_cannot_replay(self):
        record = self.tampered(
            lambda value: value["fixed_values"]["market"].update(
                INDEX_TOKEN="0x" + "33" * 20))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "sidecar.json"
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            SelectedIncreaseDecisionAdapter(
                path, ROOT / "sourcify-source-proof-507206357.json",
                ROOT / "historical-source-manifest-507206357.json",
                ROOT / "selected-order-execution-oracle.json")


if __name__ == "__main__":
    unittest.main()
