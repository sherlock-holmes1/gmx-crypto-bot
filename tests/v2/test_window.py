"""Rolling ranges, fresh configuration, and frozen resume identity."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmx_crypto_bot_v2.application.collection import parser
from gmx_crypto_bot_v2.collection.coordinator import CollectionCoordinator
from gmx_crypto_bot_v2.collection.journal import CollectionJournal
from gmx_crypto_bot_v2.collection.window import resolve_window

TEMPLATE = {
    "deployment": {"chain_id": 42161, "market_token_address": "0x" + "1" * 40},
    "contracts": {"data_store": "0x" + "2" * 40},
    "tokens": {"index": {"address": "0x" + "3" * 40}},
    "anchor_block": {"number": 1, "rpc_url": "https://example.invalid"},
    "configuration_raw": {"stale_field": "999"},
    "market_limits": {"stale": True},
}


class WindowTests(unittest.TestCase):
    def rpc(self, method, params):
        if method == "eth_chainId":
            return hex(42161)
        if method == "eth_blockNumber":
            return hex(3000)
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return {
                "number": hex(block),
                "timestamp": hex(1700000000 + block * 600),
                "hash": hex(block),
            }
        if method == "eth_call":
            self.assertEqual(params[1], hex(2990))
            return "0x" + "0" * 63 + "7"
        raise AssertionError(method)

    def test_exact_range_and_fresh_configuration(self):
        spec = resolve_window(TEMPLATE, self.rpc, 7, 10)
        window = spec["observation_window"]
        self.assertEqual(window["start_block"], 2990 - 1008)
        self.assertEqual(window["end_block"], 2990)
        self.assertEqual(window["duration_hours"], 168)
        self.assertEqual(spec["anchor_block"]["number"], 2990)
        self.assertNotIn("stale_field", spec["configuration_raw"])
        self.assertNotIn("market_limits", spec)
        self.assertEqual(
            spec["configuration_raw"]["position_impact_factor_positive"], "7"
        )
        self.assertEqual(TEMPLATE["configuration_raw"], {"stale_field": "999"})

    def test_reject_reorg_and_wrong_chain(self):
        count = 0

        def reorg(method, params):
            nonlocal count
            value = self.rpc(method, params)
            if method == "eth_getBlockByNumber" and params[0] == hex(2990):
                count += 1
                if count > 1:
                    value["hash"] = "changed"
            return value

        with self.assertRaisesRegex(ValueError, "changed"):
            resolve_window(TEMPLATE, reorg, 7, 10)
        with self.assertRaisesRegex(ValueError, "chain differs"):
            resolve_window(TEMPLATE, lambda *_: "0x1", 7, 10)

    def test_resume_freezes_selection_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = resolve_window(TEMPLATE, self.rpc, 7, 10)
            first = CollectionCoordinator(
                root,
                spec=TEMPLATE,
                last_days=7,
                archive_rpc_url="https://example.invalid",
                progress=lambda *_: None,
            )
            with patch(
                "gmx_crypto_bot_v2.collection.coordinator.resolve_window",
                return_value=selected,
            ):
                files = first._select_window()
            journal = CollectionJournal(root, first._identity())
            journal.complete("window", files)
            journal.state["identity"].update(from_block=1982, to_block=2990)
            journal.save()
            resumed = CollectionCoordinator(
                root, spec=TEMPLATE, resume=True, progress=lambda *_: None
            )
            with patch.object(
                resumed, "_rpc", side_effect=AssertionError("network used")
            ):
                resumed._select_window()
            self.assertEqual(resumed.spec, selected)
            self.assertEqual(resumed._identity()["to_block"], 2990)
            self.assertTrue(journal.completed("window"))
            wrong = CollectionCoordinator(root, spec=TEMPLATE, last_days=8, resume=True)
            with self.assertRaisesRegex(ValueError, "days differ"):
                wrong._select_window()
            saved = root / ".collection/window-selection.json"
            value = json.loads(saved.read_text())
            value["spec"]["configuration_raw"]["position_impact_factor_positive"] = "99"
            saved.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "corrupt"):
                journal.completed("window")

    def test_parser_and_invalid_days(self):
        args = parser().parse_args(["--output", "recordings/new", "--last-days", "7"])
        self.assertEqual(args.last_days, 7)
        with self.assertRaisesRegex(ValueError, "positive"):
            resolve_window(TEMPLATE, self.rpc, 0, 10)


if __name__ == "__main__":
    unittest.main()
