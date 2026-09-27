from __future__ import annotations
from types import SimpleNamespace
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from gmx_crypto_bot_v2.referral import (
    P,
    ZERO,
    ZERO_CODE,
    SIGNATURES,
    ReferralState,
    calldata,
    config_base_key,
    datastore_key,
    decode_referral_log,
    discount_amounts,
    compare_referral,
    keccak256,
    word,
)
from gmx_crypto_bot_v2.referral_backfill import collect, collect_referral_ranges
from gmx_crypto_bot_v2.collector import SourceError

A = "0x" + "11" * 20
B = "0x" + "22" * 20
R = "0x" + "33" * 20
H = "0x" + "44" * 20
D = "0x" + "55" * 20
M = "0x" + "66" * 20
CODE = "0x" + "77" * 32


def event(signature, args, block=11, tx=1, index=2):
    return {
        "address": R,
        "blockNumber": hex(block),
        "blockHash": "0xhash" + str(block),
        "transactionIndex": hex(tx),
        "logIndex": hex(index),
        "transactionHash": "0xtx",
        "topics": ["0x" + keccak256(signature.encode()).hex()],
        "data": "0x" + "".join(word(a) for a in args),
        "removed": False,
    }


class ReferralTests(unittest.TestCase):
    def test_solidity_basis_point_rounding_and_zero_code(self):
        x = discount_amounts(10**10, CODE, 1234, 3333, 0, 0, 0)
        self.assertEqual(x["referral.traderDiscountFactor"], 411 * P // 10000)
        self.assertEqual(x["referral.traderDiscountAmount"], 411000000)
        y = discount_amounts(1000, ZERO_CODE, 1000, 5000, 0, 1, P // 10)
        self.assertEqual(y["referral.totalRebateFactor"], 0)
        self.assertEqual(y["referral.affiliateRewardAmount"], 0)
        self.assertEqual(y["pro.traderDiscountAmount"], 100)
        self.assertEqual(y["protocolFeeAmount"], 900)

    def test_pro_overlap_and_minimum_affiliate_reward(self):
        for pro, affiliate, discount, protocol in [
            (5, 100, 100, 800),
            (13, 70, 130, 800),
            (18, 50, 180, 770),
            (25, 50, 250, 700),
        ]:
            with self.subTest(pro=pro):
                x = discount_amounts(1000, CODE, 2000, 5000, P // 20, 1, pro * P // 100)
                self.assertEqual(x["referral.affiliateRewardAmount"], affiliate)
                self.assertEqual(x["totalDiscountAmount"], discount)
                self.assertEqual(x["protocolFeeAmount"], protocol)
        with self.assertRaises(ValueError):
            discount_amounts(1000, CODE, 10001, 5000, 0, 0, 0)

    def test_event_decoding_and_removed_rejection(self):
        cases = [
            ("SetTraderReferralCode(address,bytes32)", [A, CODE], ("trader", A, CODE)),
            (
                "SetTier(uint256,uint256,uint256)",
                [2, 2000, 5000],
                ("tier", 2, (2000, 5000)),
            ),
            ("SetReferrerTier(address,uint256)", [B, 2], ("affiliate_tier", B, 2)),
            (
                "SetReferrerDiscountShare(address,uint256)",
                [B, 7000],
                ("share", B, 7000),
            ),
            ("RegisterCode(address,bytes32)", [B, CODE], ("code", CODE, B)),
            ("SetCodeOwner(address,address,bytes32)", [A, B, CODE], ("code", CODE, B)),
            ("GovSetCodeOwner(bytes32,address)", [CODE, B], ("code", CODE, B)),
        ]
        for signature, args, expected in cases:
            self.assertEqual(decode_referral_log(event(signature, args)), expected)
        bad = event(cases[0][0], cases[0][1])
        bad["removed"] = True
        with self.assertRaises(ValueError):
            decode_referral_log(bad)

    def fixture(self, root):
        metadata = {
            "market": {"market_token_address": M},
            "contracts": {"data_store": D, "order_handler": H},
        }
        report = {
            "complete": True,
            "gaps": [],
            "reorgs": [],
            "source_block_range": {"from": 10, "to": 20},
        }
        (root / "metadata.json").write_text(json.dumps(metadata))
        (root / "completeness-report.json").write_text(json.dumps(report))
        (root / "events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "opening_state_checkpoint",
                    "payload": {"block_number": 9, "block_hash": "0xhash9"},
                }
            )
            + "\n"
        )
        (root / "order-validation.json").write_text(
            json.dumps(
                {
                    "orders": [
                        {
                            "final_request": {"account": A, "orderType": 2},
                            "terminal": {"event_name": "OrderExecuted"},
                        }
                    ]
                }
            )
        )
        calls = {}

        def call(to, sig, arg, value):
            calls[to + ":" + calldata(sig, arg)] = "0x" + "".join(
                word(v) for v in (value if isinstance(value, tuple) else (value,))
            )

        call(H, "referralStorage()", None, R)
        call(R, "traderReferralCodes(address)", A, CODE)
        call(R, "codeOwners(bytes32)", CODE, B)
        call(R, "referrerTiers(address)", B, 0)
        call(R, "referrerDiscountShares(address)", B, 0)
        call(R, "tiers(uint256)", 0, (2000, 5000))
        call(D, "getUint(bytes32)", datastore_key("PRO_TRADER_TIER", A), 0)
        call(D, "getUint(bytes32)", datastore_key("PRO_DISCOUNT_FACTOR", 1), P // 4)
        call(
            D,
            "getUint(bytes32)",
            datastore_key("MIN_AFFILIATE_REWARD_FACTOR", 0),
            P // 20,
        )
        s = {
            "schema": "GmxReferralConfiguration",
            "version": 1,
            "market": M,
            "data_store": D,
            "order_handler": H,
            "referral_storage": R,
            "opening_block": 9,
            "opening_hash": "0xhash9",
            "end_block": 20,
            "end_hash": "0xhash20",
            "calls": calls,
            "traders": [A],
            "codes": [CODE],
            "affiliates": [B],
            "referral_tiers": [0],
            "pro_tiers": [1],
            "log_ranges": [
                {
                    "from": 10,
                    "to": 20,
                    "logs": [
                        event("SetReferrerDiscountShare(address,uint256)", [B, 7500])
                    ],
                }
            ],
            "log_block_hashes": {"11": "0xhash11"},
        }
        return metadata, s

    def test_history_custom_share_pro_tier_and_same_block_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            (root / "referral-configuration.json").write_text(json.dumps(s))
            changes = [
                {
                    "event_name": "SetUint",
                    "block_number": 11,
                    "transaction_index": 1,
                    "log_index": 4,
                    "values": {
                        "baseKey": config_base_key("PRO_TRADER_TIER"),
                        "data": "0x" + word(A),
                        "value": 1,
                    },
                }
            ]
            h = ReferralState(root, m, changes)
            self.assertEqual(h.at(A, (11, 1, 1))["share_bps"], 5000)
            self.assertEqual(h.at(A, (11, 1, 3))["share_bps"], 7500)
            self.assertEqual(h.at(A, (11, 1, 3))["pro_tier"], 0)
            self.assertEqual(h.at(A, (11, 1, 5))["pro_factor"], P // 4)
            with self.assertRaises(KeyError):
                h.at(A, (21, 0, 0))

    def test_snapshot_and_range_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            for change in ["opening", "pointer", "range", "hash"]:
                bad = copy.deepcopy(s)
                if change == "opening":
                    bad["opening_hash"] = "wrong"
                if change == "pointer":
                    bad["referral_storage"] = ZERO
                if change == "range":
                    bad["log_ranges"][0]["from"] = 11
                if change == "hash":
                    bad["log_ranges"][0]["logs"][0]["blockHash"] = "wrong"
                (root / "referral-configuration.json").write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    ReferralState(root, m, [])

    def test_comparison_detects_changed_discount_and_missing_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            (root / "referral-configuration.json").write_text(json.dumps(s))
            h = ReferralState(root, m, [])
            factors = {"position_fee_positive": P // 100}
            pos = {"values": {"sizeDeltaUsd": 100000 * P}}
            v = discount_amounts(1000, CODE, 2000, 5000, P // 20, 0, 0)
            v.update(
                trader=A,
                referralCode=CODE,
                affiliate=B,
                **{"collateralTokenPrice.min": P},
            )
            fees = {
                "values": v,
                "block_number": 10,
                "transaction_index": 0,
                "log_index": 1,
            }
            checks = {}
            errors = []
            compare_referral(
                h,
                {"account": A, "orderType": 2},
                pos,
                fees,
                factors,
                {"balance_was_improved": True},
                checks,
                errors,
            )
            self.assertEqual(set(checks.values()), {"matched"})
            self.assertFalse(errors)
            v["referral.traderDiscountAmount"] += 1
            compare_referral(
                h,
                {"account": A, "orderType": 2},
                pos,
                fees,
                factors,
                {"balance_was_improved": True},
                {},
                errors,
            )
            self.assertIn("historical_referral_discount_mismatch", errors)
            checks = {}
            compare_referral(
                None,
                {"account": A, "orderType": 2},
                pos,
                fees,
                factors,
                None,
                checks,
                [],
            )
            self.assertEqual(set(checks.values()), {"unavailable"})

    def test_range_splitting_keeps_complete_empty_intervals(self):
        class Rpc:
            def call(self, method, params):
                self.assert_method = method
                v = params[0]
                low = int(v["fromBlock"], 16)
                high = int(v["toBlock"], 16)
                if high - low > 2:
                    raise SourceError("block range limit")
                return []

        chunks = collect_referral_ranges(Rpc(), R, 10, 20, 20)
        self.assertEqual(chunks[0]["from"], 10)
        self.assertEqual(chunks[-1]["to"], 20)
        self.assertTrue(
            all(a["to"] + 1 == b["from"] for a, b in zip(chunks, chunks[1:]))
        )

    def test_backfill_derives_opening_dependencies_from_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)

            class Rpc:
                def call(self, method, params):
                    return []

                def call_many(self, method, params_list):
                    if method == "eth_getBlockByNumber":
                        return [
                            {"hash": "0xhash" + str(int(p[0], 16))} for p in params_list
                        ]
                    return [
                        s["calls"][p[0]["to"] + ":" + p[0]["data"]] for p in params_list
                    ]

            with patch(
                "gmx_crypto_bot_v2.evidence.repository.raw_logs", return_value=[]
            ):
                result = collect(
                    root,
                    Rpc(),
                    Rpc(),
                    progress=lambda *_: None,
                    requirements=SimpleNamespace(traders={A}),
                )
            self.assertEqual(result["referral_storage"], R)
            self.assertEqual(result["affiliates"], [B])
            self.assertEqual(result["pro_tiers"], [])
            (root / "referral-configuration.json").write_text(json.dumps(result))
            self.assertEqual(
                ReferralState(root, m, []).at(A, (10, 0, 0))["rebate_bps"], 2000
            )

    def test_supplement_snapshot_preferred_and_invalid_supplement_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            (root / "referral-configuration.json").write_text(json.dumps(s))
            supplemented = copy.deepcopy(s)
            supplemented["calls"][
                R + ":" + calldata("traderReferralCodes(address)", A)
            ] = "0x" + word(0)
            (root / "liquidation-referral-configuration.json").write_text(
                json.dumps(supplemented)
            )
            self.assertEqual(
                ReferralState(root, m, []).at(A, (10, 0, 0))["code"], ZERO_CODE
            )
            supplemented["opening_hash"] = "wrong"
            (root / "liquidation-referral-configuration.json").write_text(
                json.dumps(supplemented)
            )
            with self.assertRaises(ValueError):
                ReferralState(root, m, [])

    def test_backfill_includes_liquidation_only_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            (root / "order-validation.json").write_text(
                json.dumps(
                    {
                        "orders": [
                            {
                                "final_request": {"account": A, "orderType": 7},
                                "terminal": {"event_name": "OrderExecuted"},
                            }
                        ]
                    }
                )
            )

            class Rpc:
                def call(self, method, params):
                    return []

                def call_many(self, method, params_list):
                    if method == "eth_getBlockByNumber":
                        return [
                            {"hash": "0xhash" + str(int(p[0], 16))} for p in params_list
                        ]
                    return [
                        s["calls"][p[0]["to"] + ":" + p[0]["data"]] for p in params_list
                    ]

            with patch(
                "gmx_crypto_bot_v2.evidence.repository.raw_logs", return_value=[]
            ):
                result = collect(
                    root,
                    Rpc(),
                    Rpc(),
                    progress=lambda *_: None,
                    requirements=SimpleNamespace(traders={A}),
                )
            self.assertEqual(result["traders"], [A])
            (root / "liquidation-referral-configuration.json").write_text(
                json.dumps(result)
            )
            self.assertEqual(
                ReferralState(root, m, []).at(A, (10, 0, 0))["rebate_bps"], 2000
            )


if __name__ == "__main__":
    unittest.main()
