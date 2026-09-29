from types import SimpleNamespace

"""Independent numerical examples and archive/replay failure cases."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from gmx_crypto_bot_v2.checks.swaps import compare_swaps
from gmx_crypto_bot_v2.collection.swaps import collect
from gmx_crypto_bot_v2.domain.keys import config_base_key
from gmx_crypto_bot_v2.domain.referral import word
from gmx_crypto_bot_v2.domain.swap_keys import (
    MARKET_FIELDS,
    ZERO_ID,
    call_data,
    key,
    market_field_key,
    market_keys,
)
from gmx_crypto_bot_v2.models.prb import apply_exponent_factor, pow_ud60x18
from gmx_crypto_bot_v2.models.swaps import P, curve, price_swap
from gmx_crypto_bot_v2.reconstruction.swaps import SwapReplay

A = "0x" + "11" * 20
B = "0x" + "22" * 20
M = "0x" + "33" * 20
D = "0x" + "44" * 20
R = "0x" + "55" * 20
U = "0x" + "66" * 20
V = "0x" + "77" * 32
K = "0x" + "88" * 32
F = dict(
    fee_positive=P // 100,
    fee_negative=P // 50,
    impact_positive=P // 10,
    impact_negative=P // 5,
    exponent=P,
)


def event(name, index, **values):
    return dict(
        event_name=name,
        block_number=10,
        transaction_index=0,
        log_index=index,
        transaction_hash="0xtx",
        values=values,
    )


class SwapTests(unittest.TestCase):
    def test_prb_pow_domain_and_exact_binary_powers(self):
        self.assertEqual(pow_ud60x18(2 * 10**18, 2 * 10**18), 4 * 10**18)
        self.assertEqual(pow_ud60x18(4 * 10**18, 10**18 // 2), 2 * 10**18)
        self.assertEqual(pow_ud60x18(10**18, 100 * 10**18), 10**18)
        self.assertEqual(apply_exponent_factor(P - 1, 2 * P), 0)
        self.assertEqual(apply_exponent_factor(P + 1, P), P + 1)
        with self.assertRaises(ValueError):
            pow_ud60x18(10**18 - 1, 10**18)
        with self.assertRaises(ValueError):
            pow_ud60x18(2 * 10**18, 192 * 10**18)

    def test_recorded_impact_rounding_regressions_require_exact_equality(self):
        fixtures = json.loads(
            (Path(__file__).parent / "fixtures/swap-rounding.json").read_text()
        )
        for f in fixtures:
            with self.subTest(coordinate=f["coordinate"]):
                model = price_swap(**f["inputs"])
                for k, v in f["expected"].items():
                    self.assertEqual(model[k], v, k)

    def test_curve_same_side_crossing_and_positive_factor_clamp(self):
        self.assertEqual(
            curve(1000 * P, 2000 * P, 100 * P, P // 10, P // 5, P), (20 * P, True)
        )
        self.assertEqual(
            curve(2000 * P, 1000 * P, 100 * P, P // 10, P // 5, P), (-40 * P, False)
        )
        self.assertEqual(
            curve(1000 * P, 2000 * P, 800 * P, P // 10, P // 5, P), (-20 * P, True)
        )
        self.assertEqual(
            curve(1000 * P, 2000 * P, 100 * P, P, P // 5, P), (40 * P, True)
        )

    def test_positive_cap_uses_both_impact_pools_and_exact_fee_rounding(self):
        x = price_swap(100, [P] * 4, [1000, 2000], None, [7, 5], F, P // 2, P // 100)
        self.assertEqual(
            [
                x[k]
                for k in (
                    "priceImpactUsd",
                    "priceImpactAmount",
                    "tokenInPriceImpactAmount",
                    "amountInAfterFees",
                    "amountOut",
                    "pool_delta_in",
                    "pool_delta_out",
                )
            ],
            [20 * P, 5, 7, 98, 110, 106, -105],
        )
        self.assertEqual(
            [x[k] for k in ("feeReceiverAmount", "feeAmountForPool", "uiFeeAmount")],
            [0, 1, 1],
        )

    def test_negative_rounding_up_and_virtual_selection(self):
        f = dict(F, impact_negative=P // 201, impact_positive=0)
        x = price_swap(100, [P] * 4, [2000, 1000], None, [0, 0], f, P // 2, 0)
        self.assertEqual(x["priceImpactAmount"], -1)
        self.assertEqual(x["amountOut"], 97)
        square = dict(F, exponent=2 * P, impact_negative=P // 100000, impact_positive=0)
        x = price_swap(100, [P] * 4, [1100, 1000], [2000, 1000], [0, 0], square, 0, 0)
        self.assertAlmostEqual(x["market_impact_usd"] / P, -0.8, places=12)
        self.assertAlmostEqual(x["priceImpactUsd"] / P, -4.4, places=12)
        x = price_swap(100, [P] * 4, [1000, 2000], [2000, 1000], [0, 100], F, 0, 0)
        self.assertEqual(x["priceImpactUsd"], 20 * P)  # positive market skips virtual

    def fixture(self, root, virtual=ZERO_ID):
        metadata = {
            "market": {"market_token_address": M},
            "contracts": {"data_store": D},
        }
        (root / "metadata.json").write_text(json.dumps(metadata))
        (root / "completeness-report.json").write_text(
            json.dumps(
                dict(
                    complete=True,
                    gaps=[],
                    reorgs=[],
                    source_block_range={"from": 10, "to": 20},
                )
            )
        )
        (root / "events.jsonl").write_text(
            json.dumps(
                dict(
                    kind="opening_state_checkpoint",
                    payload=dict(block_number=9, block_hash="0xhash9"),
                )
            )
            + "\n"
        )
        (root / "order-validation.json").write_text(
            json.dumps(
                {
                    "orders": [
                        {
                            "observed_swap_events": [
                                event("SwapInfo", 9, market=M),
                                event(
                                    "SwapFeesCollected", 10, market=M, uiFeeReceiver=U
                                ),
                            ]
                        }
                    ]
                }
            )
        )
        calls = {}

        def add(to, sig, args, values):
            calls[to + ":" + call_data(sig, *args)] = "0x" + "".join(
                word(v) for v in values
            )

        for field, value in zip(MARKET_FIELDS, [M, A, A, B]):
            add(D, "getAddress(bytes32)", [market_field_key(M, field)], [value])
        add(D, "getBytes32(bytes32)", [key("VIRTUAL_MARKET_ID", M)], [virtual])
        settings = {
            key("SWAP_FEE_RECEIVER_FACTOR"): P // 2,
            key("MAX_UI_FEE_FACTOR"): P // 100,
            key("UI_FEE_FACTOR", U): P // 50,
        }
        settings.update({k: F[n] for n, k in market_keys(M).items()})
        for t, pool, impact in [(A, 1000, 7), (B, 2000, 5)]:
            settings[key("POOL_AMOUNT", M, t)] = pool
            settings[key("SWAP_IMPACT_POOL_AMOUNT", M, t)] = impact
        if virtual != ZERO_ID:
            for side, value in [(True, 1000), (False, 2000)]:
                settings[key("VIRTUAL_INVENTORY_FOR_SWAPS", virtual, side)] = value
        for k, v in settings.items():
            add(D, "getUint(bytes32)", [k], [v])

        class RPC:
            def call(self, method, params):
                return {"hash": "0xhash9"}

            def call_many(self, method, params):
                assert method == "eth_call" and all(p[1] == "0x9" for p in params)
                return [calls[p[0]["to"] + ":" + p[0]["data"]] for p in params]

        snapshot = collect(
            root, RPC(), requirements=SimpleNamespace(markets={M}, ui_receivers={U})
        )
        (root / "swap-opening-state.json").write_text(json.dumps(snapshot))
        return metadata, snapshot

    def timeline(self, virtual=False):
        entries = [
            event("OraclePriceUpdate", 0, token=A, minPrice=P, maxPrice=P),
            event("OraclePriceUpdate", 1, token=B, minPrice=P, maxPrice=P),
            event(
                "SwapImpactPoolAmountUpdated",
                2,
                market=M,
                token=B,
                delta=-5,
                nextValue=0,
            ),
            event(
                "SwapImpactPoolAmountUpdated",
                3,
                market=M,
                token=A,
                delta=-7,
                nextValue=0,
            ),
        ]
        if virtual:
            entries.append(
                event(
                    "VirtualSwapInventoryUpdated",
                    4,
                    market=M,
                    virtualMarketId=V,
                    isLongToken=True,
                    delta=106,
                    nextValue=1106,
                )
            )
        entries.append(
            event("PoolAmountUpdated", 5, market=M, token=A, delta=106, nextValue=1106)
        )
        if virtual:
            entries.append(
                event(
                    "VirtualSwapInventoryUpdated",
                    6,
                    market=M,
                    virtualMarketId=V,
                    isLongToken=False,
                    delta=-105,
                    nextValue=1895,
                )
            )
        entries += [
            event(
                "PoolAmountUpdated", 7, market=M, token=B, delta=-105, nextValue=1895
            ),
            event(
                "SwapInfo",
                8,
                market=M,
                orderKey=K,
                tokenIn=A,
                tokenOut=B,
                tokenInPrice=P,
                tokenOutPrice=P,
                amountIn=100,
                amountInAfterFees=98,
                amountOut=110,
                priceImpactUsd=20 * P,
                priceImpactAmount=5,
                tokenInPriceImpactAmount=7,
            ),
            event(
                "SwapFeesCollected",
                9,
                market=M,
                tradeKey=K,
                token=A,
                tokenPrice=P,
                swapFeeType=config_base_key("SWAP_FEE_TYPE"),
                feeReceiverAmount=0,
                feeAmountForPool=1,
                amountAfterFees=98,
                uiFeeReceiver=U,
                uiFeeReceiverFactor=P // 100,
                uiFeeAmount=1,
            ),
        ]
        return entries

    def test_archive_backfill_and_replay_with_virtual_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root, V)
            replay = SwapReplay(root, m)
            result = replay.replay(self.timeline(True), [], {K})[K][0]
            self.assertEqual(set(result["checks"].values()), {"matched"})
            self.assertEqual(result["pre_pool"], [1000, 2000])
            self.assertEqual(result["pre_virtual"], [1000, 2000])
            checks = {}
            errors = []
            compare_swaps(
                self.timeline(True), [result], checks, errors, {"uiFeeReceiver": U}
            )
            self.assertFalse(errors)
            self.assertEqual(set(checks.values()), {"matched"})
            compare_swaps(
                self.timeline(True), [result], checks, errors, {"uiFeeReceiver": A}
            )
            self.assertEqual(checks["historical_swap_fees"], "mismatch")

    def test_broken_continuity_missing_oracle_and_configuration_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            for mode in ("continuity", "oracle", "virtual", "config"):
                events = self.timeline()
                changes = []
                if mode == "continuity":
                    events[4]["values"]["nextValue"] += 1
                if mode == "oracle":
                    events = events[1:]
                if mode == "virtual":
                    events.append(
                        event(
                            "SetBytes32",
                            -1,
                            baseKey=config_base_key("VIRTUAL_MARKET_ID"),
                            data="0x" + word(M),
                            value=V,
                        )
                    )
                if mode == "config":
                    changes = [
                        event(
                            "SetUint",
                            -1,
                            baseKey=config_base_key("SWAP_FEE_FACTOR"),
                            data="0x" + word(M) + word(True),
                            value=P // 20,
                        )
                    ]
                result = SwapReplay(root, m).replay(events, changes, {K})[K][0]
                if mode == "config":
                    self.assertEqual(
                        result["checks"]["historical_swap_fees"], "mismatch"
                    )
                else:
                    self.assertEqual(set(result["checks"].values()), {"unavailable"})

    def test_tampered_output_and_missing_snapshot_are_not_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, s = self.fixture(root)
            events = self.timeline()
            events[-2]["values"]["amountOut"] += 5
            result = SwapReplay(root, m).replay(events, [], {K})[K][0]
            self.assertEqual(result["checks"]["independent_swap_output"], "mismatch")
            checks = {}
            compare_swaps(events, [], checks, [])
            self.assertEqual(set(checks.values()), {"unavailable"})
            s["opening_hash"] = "0xwrong"
            (root / "swap-opening-state.json").write_text(json.dumps(s))
            with self.assertRaisesRegex(ValueError, "identity"):
                SwapReplay(root, m)

    def test_two_hops_in_same_market_use_prior_hop_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, _ = self.fixture(root)
            events = self.timeline()
            events += [
                event(
                    "SwapImpactPoolAmountUpdated",
                    10,
                    market=M,
                    token=B,
                    delta=40,
                    nextValue=40,
                ),
                event(
                    "PoolAmountUpdated", 11, market=M, token=B, delta=58, nextValue=1953
                ),
                event(
                    "PoolAmountUpdated",
                    12,
                    market=M,
                    token=A,
                    delta=-57,
                    nextValue=1049,
                ),
                event(
                    "SwapInfo",
                    13,
                    market=M,
                    orderKey=K,
                    tokenIn=B,
                    tokenOut=A,
                    tokenInPrice=P,
                    tokenOutPrice=P,
                    amountIn=100,
                    amountInAfterFees=97,
                    amountOut=57,
                    priceImpactUsd=-40 * P,
                    priceImpactAmount=-40,
                    tokenInPriceImpactAmount=0,
                ),
                event(
                    "SwapFeesCollected",
                    14,
                    market=M,
                    tradeKey=K,
                    token=B,
                    tokenPrice=P,
                    swapFeeType=config_base_key("SWAP_FEE_TYPE"),
                    feeReceiverAmount=1,
                    feeAmountForPool=1,
                    amountAfterFees=97,
                    uiFeeReceiver=U,
                    uiFeeReceiverFactor=P // 100,
                    uiFeeAmount=1,
                ),
            ]
            results = SwapReplay(root, m).replay(events, [], {K})[K]
            self.assertEqual(len(results), 2)
            self.assertEqual(results[1]["pre_pool"], [1895, 1106])
            self.assertEqual(results[1]["pre_impact_pool"], [0, 0])
            self.assertTrue(
                all(set(r["checks"].values()) == {"matched"} for r in results)
            )

    def test_ui_update_applies_in_log_order_and_stale_oracle_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, _ = self.fixture(root)
            events = self.timeline()
            change = event("UiFeeFactorUpdated", -1, account=U, uiFeeFactor=0)
            result = SwapReplay(root, m).replay(events, [change], {K})[K][0]
            self.assertEqual(result["checks"]["historical_swap_fees"], "mismatch")
            change["log_index"] = 10
            result = SwapReplay(root, m).replay(events, [change], {K})[K][0]
            self.assertEqual(set(result["checks"].values()), {"matched"})
            for e in events[:2]:
                e["transaction_hash"] = "0xprevious"
            result = SwapReplay(root, m).replay(events, [], {K})[K][0]
            self.assertIn("oracle", result["unavailable_reason"])

    def test_conflicting_coordinates_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m, _ = self.fixture(root)
            events = self.timeline()
            extra = copy.deepcopy(events[0])
            extra["values"]["minPrice"] += 1
            with self.assertRaisesRegex(ValueError, "conflicting"):
                SwapReplay(root, m).replay(events + [extra], [], {K})


if __name__ == "__main__":
    unittest.main()
