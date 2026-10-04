from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gmx_crypto_bot_v2.collection.risk import collect, risk_keys, verify
from gmx_crypto_bot_v2.domain.keys import config_base_key
from gmx_crypto_bot_v2.domain.swap_keys import key


MARKET = "0x" + "11" * 20
STORE = "0x" + "22" * 20


class Archive:
    def __init__(self, keys, *, changed=True):
        self.keys = keys
        self.changed = changed
        self.headers = {block: "0x" + f"{block:064x}" for block in (9, 12, 20)}
        self.queries = []

    def call(self, method, params):
        self.queries.append((method, params))
        if method != "eth_getBlockByNumber":
            raise AssertionError(method)
        return {"hash": self.headers[int(params[0], 16)]}

    def call_many(self, method, jobs):
        if method != "eth_call":
            raise AssertionError(method)
        answers = []
        for (request, block_tag) in jobs:
            self.queries.append((method, (request, block_tag)))
            storage = "0x" + request["data"][-64:]
            field = next(name for name, key_ in self.keys.items() if key_.storage_key == storage)
            value = 12 if self.changed and field == "min_collateral_usd" and block_tag == "0x14" else 10
            answers.append("0x" + f"{value:064x}")
        return answers


class RiskConfigurationCollectionTests(unittest.TestCase):
    def fixture(self, root):
        (root / "metadata.json").write_text(json.dumps({
            "market": {"market_token_address": MARKET},
            "contracts": {"data_store": STORE},
        }))
        (root / "completeness-report.json").write_text(json.dumps({
            "complete": True, "gaps": [], "reorgs": [],
            "source_block_range": {"from": 10, "to": 20},
        }))
        (root / "events.jsonl").write_text("\n".join(json.dumps(row) for row in (
            {"kind": "opening_state_checkpoint", "payload": {
                "block_number": 9, "block_hash": "0x" + f"{9:064x}"}},
            {"kind": "block_header", "block_number": 20,
             "payload": {"hash": "0x" + f"{20:064x}"}},
        )) + "\n")

    def test_storage_keys_match_gmx_shapes(self):
        keys = risk_keys(MARKET)
        self.assertEqual(keys["min_collateral_usd"].storage_key, key("MIN_COLLATERAL_USD"))
        self.assertEqual(keys["min_position_size_usd"].storage_key, key("MIN_POSITION_SIZE_USD"))
        self.assertEqual(keys["min_collateral_factor"].storage_key,
                         key("MIN_COLLATERAL_FACTOR", MARKET))
        self.assertEqual(keys["max_position_impact_factor_for_liquidations"].storage_key,
                         key("MAX_POSITION_IMPACT_FACTOR_FOR_LIQUIDATIONS", MARKET))
        self.assertEqual(keys["max_pnl_factor_for_traders_long"].storage_key,
                         key("MAX_PNL_FACTOR", config_base_key("MAX_PNL_FACTOR_FOR_TRADERS"), MARKET, True))
        self.assertEqual(keys["max_pnl_factor_for_traders_short"].storage_key,
                         key("MAX_PNL_FACTOR", config_base_key("MAX_PNL_FACTOR_FOR_TRADERS"), MARKET, False))
        self.assertEqual(keys["min_collateral_factor_for_open_interest_multiplier_long"].storage_key,
                         key("MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER", MARKET, True))
        self.assertEqual(keys["min_collateral_factor_for_open_interest_multiplier_short"].storage_key,
                         key("MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER", MARKET, False))

    def test_collects_pinned_endpoints_and_raw_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            keys = risk_keys(MARKET)
            archive = Archive(keys)
            change = {"blockNumber": "0xc", "blockHash": archive.headers[12],
                      "transactionIndex": "0x1", "logIndex": "0x2",
                      "transactionHash": "0x" + "33" * 32, "data": "0xdata"}
            decoded = SimpleNamespace(values={
                "baseKey": keys["min_collateral_usd"].base_key,
                "data": keys["min_collateral_usd"].data,
                "value": 12,
            })
            with patch("gmx_crypto_bot_v2.collection.risk.raw_logs", return_value=[change]), \
                 patch("gmx_crypto_bot_v2.collection.risk.event_name_from_data", return_value="SetUint"), \
                 patch("gmx_crypto_bot_v2.collection.risk.decode_event_log", return_value=decoded):
                snapshot = collect(root, archive)
                verify(root, snapshot)
                snapshot["changes"][0]["coordinate"] = (12, 1, 3)
                with self.assertRaisesRegex(ValueError, "recorded SetUint"):
                    verify(root, snapshot)
                snapshot["changes"][0]["coordinate"] = (12, 1, 2)
            self.assertEqual(snapshot["opening"]["values"]["min_collateral_usd"]["value"], 10)
            self.assertEqual(snapshot["closing"]["values"]["min_collateral_usd"]["value"], 12)
            self.assertEqual(snapshot["changes"][0]["coordinate"], (12, 1, 2))
            self.assertTrue(all(tag in {"0x9", "0x14"} for method, (_, tag) in archive.queries
                                if method == "eth_call"))
            snapshot["changes"][0]["value"] = 11
            with self.assertRaisesRegex(ValueError, "closing anchor"):
                verify(root, snapshot)

    def test_rejects_unexplained_change_and_wrong_archive_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            keys = risk_keys(MARKET)
            archive = Archive(keys)
            with patch("gmx_crypto_bot_v2.collection.risk.raw_logs", return_value=[]):
                with self.assertRaisesRegex(ValueError, "closing anchor"):
                    collect(root, archive)
            archive.headers[9] = "0x" + "ff" * 32
            with self.assertRaisesRegex(ValueError, "block hash mismatch"):
                collect(root, archive)

    def test_rejects_change_log_from_different_block_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            keys = risk_keys(MARKET)
            archive = Archive(keys)
            change = {"blockNumber": "0xc", "blockHash": "0x" + "ff" * 32,
                      "transactionIndex": "0x1", "logIndex": "0x2",
                      "transactionHash": "0x" + "33" * 32, "data": "0xdata"}
            decoded = SimpleNamespace(values={
                "baseKey": keys["min_collateral_usd"].base_key,
                "data": keys["min_collateral_usd"].data,
                "value": 12,
            })
            with patch("gmx_crypto_bot_v2.collection.risk.raw_logs", return_value=[change]), \
                 patch("gmx_crypto_bot_v2.collection.risk.event_name_from_data", return_value="SetUint"), \
                 patch("gmx_crypto_bot_v2.collection.risk.decode_event_log", return_value=decoded):
                with self.assertRaisesRegex(ValueError, "log block hash mismatch"):
                    collect(root, archive)


if __name__ == "__main__":
    unittest.main()
