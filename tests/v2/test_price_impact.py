from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gmx_crypto_bot_v2.price_impact import (
    FLOAT_PRECISION,
    HistoricalFactor,
    apply_exponent_factor,
    balance_impact,
    config_base_key,
    config_market_side_data,
    keccak256,
    load_factor_histories,
    predict_current_impact,
)
from gmx_crypto_bot_v2.impact_backfill import fetch_opening_impact_factors
from gmx_crypto_bot_v2.validator import (
    _attach_pre_impact_pool,
    _attach_pre_position_state,
    _attach_pre_virtual_inventory,
    _compare_independent_price_impact,
)


class PriceImpactPrimitiveTests(unittest.TestCase):
    def test_ethereum_keccak_and_recorded_config_keys(self) -> None:
        self.assertEqual(
            keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )
        self.assertEqual(
            keccak256(b"abc").hex(),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
        )
        self.assertEqual(
            config_base_key("POSITION_IMPACT_FACTOR"),
            "0xdbe66c23ce3688bc34131316a8b4dd967631de35afb1dbb972bc1b7c71c4a18b",
        )
        self.assertEqual(
            config_base_key("POSITION_IMPACT_EXPONENT_FACTOR"),
            "0xdd130ae153fcacdeb3063e386277de2d305638932d8de421f5c79f3919eea233",
        )
        self.assertEqual(
            config_market_side_data("0x70d95587d40a2caf56bd97485ab3eec10bee6336", True)[
                -64:
            ],
            "0" * 63 + "1",
        )

    def test_same_side_and_crossover_impact(self) -> None:
        linear = lambda diff, exponent: diff if exponent == FLOAT_PRECISION else None
        p, improved = balance_impact(
            200,
            100,
            -50,
            True,
            FLOAT_PRECISION // 10,
            FLOAT_PRECISION // 5,
            FLOAT_PRECISION,
            FLOAT_PRECISION,
            linear,
        )
        self.assertEqual((p, improved), (5, True))
        p, improved = balance_impact(
            200,
            100,
            150,
            True,
            FLOAT_PRECISION // 10,
            FLOAT_PRECISION // 5,
            FLOAT_PRECISION,
            FLOAT_PRECISION,
            linear,
        )
        self.assertEqual((p, improved), (-30, False))
        p, improved = balance_impact(
            200,
            100,
            150,
            False,
            FLOAT_PRECISION // 10,
            FLOAT_PRECISION // 5,
            FLOAT_PRECISION,
            FLOAT_PRECISION,
            linear,
        )
        self.assertEqual((p, improved), (0, True))

    def test_decimal_exponent_bridge_and_virtual_worst_case(self) -> None:
        self.assertEqual(
            apply_exponent_factor(FLOAT_PRECISION - 1, 2 * FLOAT_PRECISION), 0
        )
        self.assertEqual(
            apply_exponent_factor(3 * FLOAT_PRECISION, FLOAT_PRECISION),
            3 * FLOAT_PRECISION,
        )
        self.assertEqual(
            apply_exponent_factor(3 * FLOAT_PRECISION, 2 * FLOAT_PRECISION),
            9 * FLOAT_PRECISION,
        )
        factors = {
            "position_impact_factor_positive": FLOAT_PRECISION // 10,
            "position_impact_factor_negative": FLOAT_PRECISION // 5,
            "position_impact_exponent_factor_positive": FLOAT_PRECISION,
            "position_impact_exponent_factor_negative": FLOAT_PRECISION,
        }
        result = predict_current_impact(
            200, 100, 500, 150, True, FLOAT_PRECISION, FLOAT_PRECISION, factors
        )
        self.assertEqual(result["market_impact_usd"], -30 * FLOAT_PRECISION)
        self.assertEqual(
            result["selected_impact_usd"],
            min(result["market_impact_usd"], result["virtual_impact_usd"]),
        )

    def test_historical_factor_never_backfills_before_first_write(self) -> None:
        history = HistoricalFactor(120, 8, (((101, 2, 4), 7), ((110, 1, 0), 8)))
        self.assertIsNone(history.at((100, 9, 9)))
        self.assertEqual(history.at((101, 2, 4)), 7)
        self.assertEqual(history.at((109, 0, 0)), 7)
        self.assertEqual(history.at((110, 1, 0)), 8)
        self.assertEqual(history.at((120, 0, 0)), 8)
        self.assertIsNone(history.at((121, 0, 0)))
        self.assertEqual(HistoricalFactor(120, 8, ()).at((100, 0, 0)), 8)
        self.assertEqual(
            HistoricalFactor(120, 8, (((110, 1, 0), 8),), 7).at((100, 0, 0)), 7
        )
        with self.assertRaises(ValueError):
            HistoricalFactor(120, 8, (((110, 1, 0), 7),))
        with self.assertRaises(ValueError):
            HistoricalFactor(120, 8, (), 7)

    def test_pre_order_open_interest_reverses_own_emitted_delta(self) -> None:
        common = {"block_number": 2, "transaction_index": 0, "transaction_hash": "0xtx"}
        update = {
            **common,
            "log_index": 2,
            "event_name": "OpenInterestInTokensUpdated",
            "values": {
                "collateralToken": "0xusdc",
                "isLong": True,
                "delta": 10,
                "nextValue": 110,
            },
        }
        position = {
            **common,
            "log_index": 3,
            "event_name": "PositionIncrease",
            "values": {
                "positionKey": "0xposition",
                "collateralToken": "0xusdc",
                "isLong": True,
                "sizeDeltaInTokens": 10,
                "sizeInUsd": 100,
                "sizeInTokens": 10,
            },
        }
        _attach_pre_position_state(
            [position],
            {},
            [],
            {},
            open_interest_events=[update],
            opening_open_interest_tokens={"0xusdc:long": 100, "0xusdc:short": 80},
        )
        self.assertEqual(
            position["pre_open_interest_tokens"], {"long": 100, "short": 80}
        )
        self.assertTrue(position["open_interest_update_matches"])

    def test_virtual_inventory_uses_own_delta_and_detects_gap(self) -> None:
        common = {"block_number": 2, "transaction_index": 0, "transaction_hash": "0xtx"}
        virtual = {
            **common,
            "log_index": 2,
            "event_name": "VirtualPositionInventoryUpdated",
            "values": {"delta": -10, "nextValue": 90},
        }
        position = {
            **common,
            "log_index": 3,
            "event_name": "PositionIncrease",
            "values": {"sizeDeltaInTokens": 10, "isLong": True},
        }
        _attach_pre_virtual_inventory([position], [virtual])
        self.assertEqual(position["pre_virtual_inventory_tokens"], 100)
        self.assertTrue(position["virtual_inventory_update_matches"])
        self.assertTrue(position["virtual_inventory_continuous"])
        another = {
            **common,
            "block_number": 3,
            "transaction_hash": "0xtx2",
            "log_index": 1,
            "event_name": "VirtualPositionInventoryUpdated",
            "values": {"delta": 5, "nextValue": 200},
        }
        position2 = {
            **common,
            "block_number": 3,
            "transaction_hash": "0xtx2",
            "log_index": 2,
            "event_name": "PositionIncrease",
            "values": {"sizeDeltaInTokens": 5, "isLong": False},
        }
        _attach_pre_virtual_inventory([position, position2], [virtual, another])
        self.assertFalse(position2["virtual_inventory_continuous"])
        self.assertNotIn("pre_virtual_inventory_tokens", position2)

    def test_impact_pool_uses_pre_update_amount_for_same_order(self) -> None:
        common = {"block_number": 2, "transaction_index": 0, "transaction_hash": "0xtx"}
        update = {
            **common,
            "log_index": 2,
            "event_name": "PositionImpactPoolAmountUpdated",
            "values": {"delta": -10, "nextValue": 90},
        }
        position = {
            **common,
            "log_index": 3,
            "event_name": "PositionDecrease",
            "values": {},
        }
        later = {
            **common,
            "block_number": 3,
            "transaction_hash": "0xlater",
            "log_index": 2,
            "event_name": "PositionIncrease",
            "values": {},
        }
        _attach_pre_impact_pool([position, later], [update])
        self.assertEqual(position["pre_impact_pool_amount"], 100)
        self.assertEqual(later["pre_impact_pool_amount"], 90)
        self.assertTrue(position["impact_pool_continuous"])

    def test_independent_impact_checks_current_curve_and_decrease_pool_cap(
        self,
    ) -> None:
        factors = {
            "position_impact_factor_positive": FLOAT_PRECISION // 10,
            "position_impact_factor_negative": FLOAT_PRECISION // 5,
            "position_impact_exponent_factor_positive": FLOAT_PRECISION,
            "position_impact_exponent_factor_negative": FLOAT_PRECISION,
            "max_position_impact_factor_positive": FLOAT_PRECISION,
            "max_position_impact_factor_negative": FLOAT_PRECISION,
            "max_lendable_impact_factor": 0,
            "max_lendable_impact_usd": 0,
        }
        common = {
            "pre_open_interest_tokens": {"long": 200, "short": 100},
            "pre_virtual_inventory_tokens": 500,
            "pre_impact_pool_amount": 3,
            "open_interest_update_matches": True,
            "virtual_inventory_update_matches": True,
            "virtual_inventory_continuous": True,
            "impact_pool_continuous": True,
        }
        prices = {
            "indexTokenPrice.min": FLOAT_PRECISION,
            "indexTokenPrice.max": FLOAT_PRECISION,
        }
        increase = {
            **common,
            "event_name": "PositionIncrease",
            "values": {
                **prices,
                "isLong": True,
                "sizeDeltaInTokens": 150,
                "sizeDeltaUsd": 150 * FLOAT_PRECISION,
                "pendingPriceImpactUsd": -30 * FLOAT_PRECISION,
            },
        }
        checks, mismatches = {}, []
        self.assertIsNotNone(
            _compare_independent_price_impact(increase, factors, checks, mismatches)
        )
        self.assertEqual(checks["independent_price_impact"], "matched")
        self.assertEqual(mismatches, [])
        altered = {
            **increase,
            "values": {
                **increase["values"],
                "pendingPriceImpactUsd": -30 * FLOAT_PRECISION + 2 * 10**18,
            },
        }
        checks, mismatches = {}, []
        _compare_independent_price_impact(altered, factors, checks, mismatches)
        self.assertEqual(checks["independent_price_impact"], "mismatch")
        self.assertEqual(mismatches, ["independent_price_impact_mismatch"])
        decrease = {
            **common,
            "event_name": "PositionDecrease",
            "values": {
                **prices,
                "isLong": True,
                "sizeDeltaInTokens": 50,
                "sizeDeltaUsd": 50 * FLOAT_PRECISION,
                "priceImpactUsd": 5 * FLOAT_PRECISION,
                "proportionalPendingImpactUsd": 0,
                "totalImpactUsd": 3 * FLOAT_PRECISION,
                "values.priceImpactDiffUsd": 0,
            },
        }
        checks, mismatches = {}, []
        model = _compare_independent_price_impact(decrease, factors, checks, mismatches)
        self.assertEqual(model["total_impact_usd"], 3 * FLOAT_PRECISION)
        self.assertEqual(checks["independent_price_impact"], "matched")
        self.assertEqual(mismatches, [])

    def test_archive_backfill_checks_opening_block_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recording = Path(temporary)
            (recording / "metadata.json").write_text(
                json.dumps(
                    {
                        "market": {"market_token_address": "0x" + "1" * 40},
                        "contracts": {"data_store": "0x" + "2" * 40},
                    }
                )
            )
            (recording / "events.jsonl").write_text(
                json.dumps(
                    {
                        "kind": "opening_state_checkpoint",
                        "payload": {"block_number": 100, "block_hash": "0xabc"},
                    }
                )
                + "\n"
            )
            calls = []

            def rpc(method, params):
                calls.append((method, params))
                return (
                    {"hash": "0xabc"}
                    if method == "eth_getBlockByNumber"
                    else "0x" + "0" * 63 + "7"
                )

            result = fetch_opening_impact_factors(recording, rpc)
            self.assertEqual(len(result["factors"]), 4)
            self.assertEqual(set(result["factors"].values()), {7})
            self.assertEqual(
                [method for method, _ in calls],
                ["eth_getBlockByNumber"] + ["eth_call"] * 4 + ["eth_getBlockByNumber"],
            )
            self.assertTrue(
                all(
                    params[-1] == "0x64"
                    for method, params in calls
                    if method == "eth_call"
                )
            )
            with self.assertRaisesRegex(ValueError, "block hash"):
                fetch_opening_impact_factors(
                    recording, lambda method, params: {"hash": "0xother"}
                )

    def test_archive_sidecar_supplies_opening_factor_without_lookahead(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recording = Path(temporary)
            market = "0x" + "1" * 40
            metadata = {
                "market": {"market_token_address": market},
                "pinned_configuration_anchor_block": 120,
                "pinned_configuration_raw": {"position_impact_factor_positive": 7},
            }
            (recording / "events.jsonl").write_text(
                json.dumps(
                    {
                        "kind": "opening_state_checkpoint",
                        "payload": {"block_number": 100, "block_hash": "0xabc"},
                    }
                )
                + "\n"
            )
            sidecar = {
                "schema": "GmxImpactOpeningConfiguration",
                "version": 1,
                "block_number": 100,
                "block_hash": "0xabc",
                "market": market,
                "factors": {"position_impact_factor_positive": 7},
            }
            (recording / "impact-opening-configuration.json").write_text(
                json.dumps(sidecar)
            )
            history = load_factor_histories(recording, metadata)[
                "position_impact_factor_positive"
            ]
            self.assertEqual(history.at((100, 0, 0)), 7)
            sidecar["block_hash"] = "0xother"
            (recording / "impact-opening-configuration.json").write_text(
                json.dumps(sidecar)
            )
            with self.assertRaisesRegex(ValueError, "checkpoint"):
                load_factor_histories(recording, metadata)


if __name__ == "__main__":
    unittest.main()
