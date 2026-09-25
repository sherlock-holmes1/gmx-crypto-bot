from __future__ import annotations

import sys
import tempfile
import unittest
import base64
import gzip
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gmx_crypto_bot.collector import (
    GmxCollector,
    PublicJsonRpc,
    RangeGap,
    SourceError,
    adaptive_ranges,
    event_name,
    main,
    redact_rpc_endpoint,
    target_snapshot,
)
from gmx_crypto_bot.artifacts import RawArtifactStore
from gmx_crypto_bot.checkpoint import _decode_order, normalize_recorded_order_checkpoint
from gmx_crypto_bot.recording import load_recording
from gmx_crypto_bot.validator import (
    FLOAT_PRECISION,
    FUNDING_PRECISION,
    _attach_pre_position_state,
    _associate_execution_fees,
    _compare_execution_fee_events,
    _compare_collateral_conversion,
    _compare_decrease_settlement,
    ARBITRUM_MULTICHAIN_VAULT,
    _compare_execution_price,
    _compare_fee_math,
    _compare_position_math,
    _single_update_topup,
    _reconstruct_terminal_reason,
    _validate_order,
)
from gmx_crypto_bot.event_decoder import DecodedEventLog


class RawArtifactStoreTests(unittest.TestCase):
    def test_responses_are_preserved_in_rotated_compressed_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            recording = Path(temporary_directory) / "recording"
            recording.mkdir()
            store = RawArtifactStore(recording, max_bundle_bytes=1)
            first = store.response("rpc-eth_getLogs", {"id": 1}, b'{"first":true}')
            second = store.response("rpc-eth_getBlockByNumber", {"id": 2}, b'{"second":true}')
            store.close()

            self.assertEqual(first, "raw/rpc-000001.jsonl.gz#1")
            self.assertEqual(second, "raw/rpc-000002.jsonl.gz#1")
            bundles = sorted((recording / "raw").glob("rpc-*.jsonl.gz"))
            self.assertEqual([bundle.name for bundle in bundles], ["rpc-000001.jsonl.gz", "rpc-000002.jsonl.gz"])
            with gzip.open(bundles[0], "rt", encoding="utf-8") as stream:
                first_record = json.loads(stream.readline())
            self.assertEqual(base64.b64decode(first_record["body_base64"]), b'{"first":true}')

            manifest = [json.loads(line) for line in (recording / "raw" / "manifest.jsonl").read_text().splitlines()]
            self.assertEqual(manifest[0]["artifact"], first)
            self.assertEqual(manifest[0]["sha256"], first_record["body_sha256"])


class OrderValidationTests(unittest.TestCase):
    def test_deployed_order_tuple_has_thirteen_number_slots(self) -> None:
        words = [0] * 30
        words[0] = 32
        words[1] = 19 * 32  # addresses offset from tuple root
        words[2] = 6  # order type
        words[13] = 0  # source chain
        words[14] = 0  # extra deployed numeric slot
        words[15:19] = [1, 0, 0, 1]  # long, unwrap, frozen, auto-cancel
        words[19] = 28 * 32  # actual data-list offset
        words[27] = 8 * 32  # swap-path offset from addresses base
        encoded = "0x" + "".join(f"{word:064x}" for word in words)

        order = _decode_order(encoded)

        self.assertTrue(order["isLong"])
        self.assertTrue(order["autoCancel"])
        self.assertFalse(order["shouldUnwrapNativeToken"])
        self.assertEqual(order["dataList"], [])

    def test_order_decoder_accepts_twelve_number_slots(self) -> None:
        words = [0] * 29
        words[0] = 32
        words[1] = 18 * 32
        words[2] = 2
        words[14:18] = [1, 0, 0, 0]
        words[18] = 27 * 32
        words[26] = 8 * 32
        encoded = "0x" + "".join(f"{word:064x}" for word in words)

        order = _decode_order(encoded)

        self.assertTrue(order["isLong"])
        self.assertEqual(order["orderType"], 2)
        self.assertEqual(order["dataList"], [])

    def test_legacy_checkpoint_flags_are_corrected_without_losing_raw_words(self) -> None:
        words = [0] * 28
        words[0] = 6
        words[13:17] = [1, 0, 0, 1]
        words[17] = 28 * 32
        raw = {"orderType": 6, "isLong": False, "shouldUnwrapNativeToken": True,
               "dataList": [f"0x{word:064x}" for word in words]}

        corrected = normalize_recorded_order_checkpoint(raw)

        self.assertTrue(corrected["isLong"])
        self.assertFalse(corrected["shouldUnwrapNativeToken"])
        self.assertTrue(corrected["autoCancel"])
        self.assertTrue(corrected["data_list_unavailable"])
        self.assertEqual(corrected["legacy_decoder_words"], raw["dataList"])

    def test_auto_updated_size_is_compared_to_observed_execution(self) -> None:
        request = _validation_entry("OrderCreated", 1, {
            "account": "0xaccount", "sizeDeltaUsd": 100, "acceptablePrice": 0,
            "orderType": 4, "isLong": False, "initialCollateralToken": "0xusdc",
        })
        lifecycle = [
            _validation_entry("OrderSizeDeltaAutoUpdated", 2, {"nextSizeDeltaUsd": 75}),
            _validation_entry("OrderExecuted", 3, {"account": "0xaccount"}),
        ]
        observed = [_validation_entry("PositionDecrease", 3, {
            "sizeDeltaUsd": 75, "executionPrice": 100, "isLong": False, "collateralToken": "0xusdc",
        })]
        receipts = {"0xtx-3": {"payload": {"status": "0x1", "gas_used": "0x100"}}}

        result = _validate_order("0xorder", request, lifecycle, observed, receipts)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["final_request"]["sizeDeltaUsd"], 75)

    def test_cancelled_order_checks_terminal_receipt(self) -> None:
        request = _validation_entry("OrderCreated", 1, {"account": "0xaccount"})
        cancellation = _validation_entry("OrderCancelled", 2, {
            "account": "0xaccount", "reason": "USER_INITIATED_CANCEL",
        })
        receipts = {"0xtx-2": {"payload": {"status": "0x1", "gas_used": "0x200"}}}

        result = _validate_order("0xorder", request, [cancellation], [], receipts)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["checks"]["receipt"], "matched")
        self.assertEqual(result["checks"]["gas_used"], "matched")

    def test_terminal_reason_reconstruction_uses_independent_evidence(self) -> None:
        request = {"account": "0xaccount", "market": "0xmarket", "initialCollateralToken": "0xusdc",
                   "isLong": False, "autoCancel": True, "orderType": 5}
        terminal = _validation_entry("OrderCancelled", 3, {"reason": "AUTO_CANCEL", "reasonBytes": "0x"})
        closed = _validation_entry("PositionDecrease", 3, {
            "account": "0xaccount", "market": "0xmarket", "collateralToken": "0xusdc",
            "isLong": False, "sizeInUsd": 0,
        })
        closed["log_index"] = 1
        terminal["log_index"] = 2
        self.assertEqual(_reconstruct_terminal_reason(request, terminal, [], {}, [closed], []), "matched")
        mismatches: list[str] = []
        self.assertEqual(_reconstruct_terminal_reason(request, terminal, [], {}, [], mismatches), "mismatch")
        self.assertEqual(mismatches, ["auto_cancel_without_matching_position_close"])

        terminal["values"] = {"reason": "", "reasonBytes": "0x4dfbbff3"}
        request["orderType"] = 4
        self.assertEqual(_reconstruct_terminal_reason(request, terminal, [], {}, [closed], []), "matched")
        active = dict(closed, values=dict(closed["values"], sizeInUsd=10))
        self.assertEqual(_reconstruct_terminal_reason(request, terminal, [], {}, [active], []), "mismatch")

        terminal["values"] = {"reason": "USER_INITIATED_CANCEL", "reasonBytes": "0x"}
        keeper = _validation_entry("KeeperExecutionFee", 3, {"keeper": "0xaccount"})
        self.assertEqual(_reconstruct_terminal_reason(request, terminal, [keeper], {}, [], []), "matched")

        request["acceptablePrice"] = 100
        terminal["values"] = {"reason": "", "reasonBytes": "0xe09ad0e9" + f"{101:064x}{100:064x}"}
        self.assertEqual(_reconstruct_terminal_reason(request, terminal, [], {}, [], []), "matched")

    def test_swapped_collateral_is_reconciled_to_position_and_prices(self) -> None:
        request = {"initialCollateralToken": "0xbtc", "initialCollateralDeltaAmount": 10,
                   "minOutputAmount": 18}
        position = {"values": {"collateralToken": "0xusdc", "collateralDeltaAmount": 16}}
        fees = {"values": {"totalCostAmount": 2}}
        swap = _validation_entry("SwapInfo", 2, {
            "tokenIn": "0xbtc", "tokenOut": "0xusdc", "amountIn": 10,
            "amountInAfterFees": 10, "tokenInPrice": 2, "tokenOutPrice": 1,
            "priceImpactAmount": -1, "tokenInPriceImpactAmount": 0, "amountOut": 18,
        })
        checks: dict[str, str] = {}
        mismatches: list[str] = []
        _compare_collateral_conversion(request, position, fees, [swap], checks, mismatches)
        self.assertEqual(checks, {"collateral_conversion": "matched", "swap_output_arithmetic": "matched"})
        self.assertEqual(mismatches, [])
        swap["values"]["amountOut"] = 20
        _compare_collateral_conversion(request, position, fees, [swap], checks, mismatches)
        self.assertIn("collateral_conversion_mismatch", mismatches)
        self.assertIn("swap_output_arithmetic_mismatch", mismatches)

    def test_full_decrease_settlement_matches_collateral_and_receipt(self) -> None:
        token = "0xweth"
        request = {"initialCollateralDeltaAmount": 100, "decreasePositionSwapType": 0,
                   "srcChainId": 0, "receiver": "0xtrader", "minOutputAmount": 0}
        position = _validation_entry("PositionDecrease", 2, {
            "isLong": True, "collateralToken": token, "basePnlUsd": -20,
            "uncappedBasePnlUsd": -20, "totalImpactUsd": -4,
            "sizeDeltaUsd": 10, "sizeInUsd": 0, "sizeInTokens": 0,
            "collateralAmount": 0, "collateralDeltaAmount": 100,
        })
        position["pre_position"] = {"collateralAmount": 100, "sizeInUsd": 10}
        fees = _validation_entry("PositionFeesCollected", 2, {
            "collateralTokenPrice.min": 2, "collateralTokenPrice.max": 2,
            "fundingFeeAmount": 1, "totalCostAmount": 3,
        })
        payout = {"event_name": "ERC20Transfer", "token": token, "amount": 85,
                  "to": "0xtrader", "from": "0xmarket"}
        metadata = {"tokens": {"long": {"address": token}, "index": {"address": token}}}
        checks = {"uncapped_pnl": "matched"}
        mismatches: list[str] = []
        model = _compare_decrease_settlement(request, position, fees, [], [payout], metadata, checks, mismatches)
        self.assertEqual(model["output_amount"], 85)
        self.assertEqual(model["remaining_collateral"], 0)
        self.assertEqual(mismatches, [])
        self.assertEqual({checks[name] for name in (
            "decrease_collateral_and_cash", "net_realized_pnl", "output_amounts"
        )}, {"matched"})

        request["data_list_unavailable"] = True
        payout = _validation_entry("MultichainTransferIn", 2, {
            "token": token, "amount": 85, "account": "0xtrader", "srcChainId": 8453,
        })
        transfer = {"event_name": "ERC20Transfer", "token": token, "amount": 85,
                    "to": ARBITRUM_MULTICHAIN_VAULT, "from": "0xmarket"}
        model = _compare_decrease_settlement(request, position, fees, [], [transfer, payout], metadata, checks, mismatches)
        self.assertEqual(model["payout_route"], "checkpoint_route_inferred_from_receipt")
        self.assertEqual(mismatches, [])
        _compare_decrease_settlement(request, position, fees, [], [payout], metadata, checks, mismatches)
        self.assertIn("decrease_output_amount_mismatch", mismatches)

    def test_execution_fees_follow_each_terminal_in_batched_transaction(self) -> None:
        terminal_a = _validation_entry("OrderExecuted", 10, {"key": "0xa"})
        terminal_b = _validation_entry("OrderCancelled", 10, {"key": "0xb"})
        keeper_a = _validation_entry("KeeperExecutionFee", 10, {"executionFeeAmount": 40})
        refund_a = _validation_entry("ExecutionFeeRefund", 10, {"refundFeeAmount": 60})
        keeper_b = _validation_entry("KeeperExecutionFee", 10, {"executionFeeAmount": 20})
        refund_b = _validation_entry("ExecutionFeeRefund", 10, {"refundFeeAmount": 80})
        for index, entry in enumerate((terminal_a, keeper_a, refund_a, terminal_b, keeper_b, refund_b)):
            entry["log_index"] = index
        timeline = {"0xtx-10": [refund_b, terminal_b, keeper_a, terminal_a, keeper_b, refund_a]}

        fees = _associate_execution_fees(timeline)
        self.assertEqual(fees[("0xtx-10", 0)], [keeper_a, refund_a])
        self.assertEqual(fees[("0xtx-10", 3)], [keeper_b, refund_b])
        checks: dict[str, str] = {}
        mismatches: list[str] = []
        _compare_execution_fee_events({"executionFee": 100}, fees[("0xtx-10", 3)], checks, mismatches)
        self.assertEqual(checks["execution_fee_event_balance"], "matched")
        self.assertEqual(mismatches, [])
        refund_b["values"]["refundFeeAmount"] = 79
        _compare_execution_fee_events({"executionFee": 100}, fees[("0xtx-10", 3)], {}, mismatches)
        self.assertIn("execution_fee_event_balance_mismatch", mismatches)

    def test_update_receipt_topup_is_counted_once(self) -> None:
        wnt = "0x" + "ab" * 20
        logs = [
            {"logIndex": "0x3", "address": wnt,
             "topics": [
                 "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                 "0x" + "00" * 32,
                 "0x" + "00" * 12 + "31ef83a530fde1b38ee9a18093a333d8bbbc40d5",
             ], "data": "0x5"},
            {"logIndex": "0x4", "address": "0x" + "cd" * 20,
             "topics": [], "data": "update"},
        ]
        with patch("gmx_crypto_bot.validator.event_name_from_data", side_effect=lambda value: "OrderUpdated" if value == "update" else None):
            self.assertEqual(_single_update_topup(logs, 4, wnt), 5)
            self.assertIsNone(_single_update_topup(logs + [logs[1]], 4, wnt))
        created = {"logIndex": "0x2", "address": "0x" + "cd" * 20,
                   "topics": [], "data": "created"}
        with patch("gmx_crypto_bot.validator.event_name_from_data", side_effect=lambda value: {
            "update": "OrderUpdated", "created": "OrderCreated",
        }.get(value)):
            self.assertEqual(_single_update_topup([created, *logs], 4, wnt), 5)
            earlier_transfer = dict(logs[0], logIndex="0x1")
            self.assertEqual(_single_update_topup([earlier_transfer, created, logs[1]], 4, wnt), 0)

    def test_pre_position_uses_checkpoint_and_canonical_updates(self) -> None:
        first = _validation_entry("PositionDecrease", 12, {
            "positionKey": "0xposition", "isLong": True, "sizeInUsd": 60,
            "sizeInTokens": 6, "collateralAmount": 50,
        })
        second = _validation_entry("PositionDecrease", 13, {
            "positionKey": "0xposition", "isLong": True, "sizeInUsd": 0,
            "sizeInTokens": 0, "collateralAmount": 0,
        })
        borrowing = _validation_entry("CumulativeBorrowingFactorUpdated", 11, {
            "isLong": True, "nextValue": 25,
        })

        _attach_pre_position_state(
            [second, first],
            {"0xposition": {"sizeInUsd": 100, "sizeInTokens": 10, "collateralAmount": 80, "isLong": True}},
            [borrowing],
            {"long": {"cumulative_factor": {"nextCumulativeBorrowingFactor": 20}}},
        )

        self.assertEqual(first["pre_position"]["sizeInUsd"], 100)
        self.assertEqual(first["cumulative_borrowing_factor"], 25)
        self.assertEqual(second["pre_position"]["sizeInUsd"], 60)

    def test_pre_position_tracks_pending_impact_across_increase_and_decrease(self) -> None:
        increase = _validation_entry("PositionIncrease", 11, {
            "positionKey": "0xposition", "isLong": True, "sizeInUsd": 20,
            "sizeInTokens": 20, "pendingPriceImpactAmount": 3,
        })
        decrease = _validation_entry("PositionDecrease", 12, {
            "positionKey": "0xposition", "isLong": True, "sizeDeltaUsd": 10,
            "sizeInUsd": 10, "sizeInTokens": 10,
        })
        next_decrease = _validation_entry("PositionDecrease", 13, {
            "positionKey": "0xposition", "isLong": True, "sizeDeltaUsd": 10,
            "sizeInUsd": 0, "sizeInTokens": 0,
        })

        _attach_pre_position_state(
            [next_decrease, decrease, increase],
            {"0xposition": {"sizeInUsd": 10, "sizeInTokens": 10, "pendingImpactAmount": -5}},
            [], {},
        )

        self.assertEqual(decrease["pre_position"]["pendingImpactAmount"], -2)
        self.assertEqual(next_decrease["pre_position"]["pendingImpactAmount"], -1)

    def test_fee_math_uses_pre_position_and_reports_discrepancy(self) -> None:
        size = 100 * FLOAT_PRECISION
        price = 10**24
        position = {
            "event_name": "PositionDecrease",
            "values": {"sizeDeltaUsd": size},
            "pre_position": {
                "sizeInUsd": size, "borrowingFactor": 0, "fundingFeeAmountPerSize": 0,
            },
        }
        fee_values = {
            "collateralTokenPrice.min": price,
            "positionFeeFactor": 10**28,
            "positionFeeAmount": 1_000_000,
            "borrowingFeeUsd": size // 100,
            "borrowingFeeAmount": 1_000_000,
            "latestFundingFeeAmountPerSize": FUNDING_PRECISION // size,
            "fundingFeeAmount": 1,
            "liquidationFeeAmount": 0,
            "uiFeeAmount": 0,
            "referral.traderDiscountAmount": 0,
            "totalCostAmount": 2_000_001,
        }
        fees = {"values": fee_values, "cumulative_borrowing_factor": 10**28}
        checks: dict[str, str] = {}
        mismatches: list[str] = []

        _compare_fee_math({"orderType": 4}, position, fees, checks, mismatches)

        self.assertEqual(mismatches, [])
        self.assertEqual(checks["position_fee"], "matched")
        self.assertEqual(checks["borrowing_fee_usd"], "matched")
        self.assertEqual(checks["funding_fee"], "matched")
        fee_values["borrowingFeeAmount"] += 1
        _compare_fee_math({"orderType": 4}, position, fees, {}, mismatches)
        self.assertIn("borrowing_fee_amount_mismatch", mismatches)

    def test_execution_price_uses_pending_impact_for_increase(self) -> None:
        values = {
            "sizeDeltaUsd": 1000, "sizeDeltaInTokens": 10,
            "pendingPriceImpactAmount": 2, "isLong": True,
            "indexTokenPrice.max": 100, "executionPrice": 83,
        }
        checks: dict[str, str] = {}
        mismatches: list[str] = []

        _compare_execution_price(values, 0, 0, checks, mismatches, increase=True)

        self.assertEqual(checks["execution_price"], "matched")
        self.assertEqual(mismatches, [])

    def test_claimable_funding_rounds_down(self) -> None:
        size = 2 * FLOAT_PRECISION
        position = {"event_name": "PositionDecrease", "values": {"sizeDeltaUsd": size},
                    "pre_position": {"sizeInUsd": size, "borrowingFactor": 0,
                                     "fundingFeeAmountPerSize": 0,
                                     "longTokenClaimableFundingAmountPerSize": 0,
                                     "shortTokenClaimableFundingAmountPerSize": 0}}
        values = {"collateralTokenPrice.min": 10**24, "positionFeeFactor": 0,
                  "positionFeeAmount": 0, "latestFundingFeeAmountPerSize": 0,
                  "fundingFeeAmount": 0, "latestLongTokenClaimableFundingAmountPerSize": FUNDING_PRECISION // (size * 2),
                  "latestShortTokenClaimableFundingAmountPerSize": FUNDING_PRECISION // (size * 2),
                  "claimableLongTokenAmount": 0, "claimableShortTokenAmount": 0,
                  "borrowingFeeAmount": 0, "liquidationFeeAmount": 0,
                  "uiFeeAmount": 0, "totalCostAmount": 0}
        checks: dict[str, str] = {}
        mismatches: list[str] = []
        _compare_fee_math({"orderType": 4}, position,
                          {"values": values, "cumulative_borrowing_factor": 0}, checks, mismatches)
        self.assertEqual(checks["claimable_long_funding"], "matched")
        self.assertEqual(checks["claimable_short_funding"], "matched")
        values["claimableLongTokenAmount"] = 1
        _compare_fee_math({"orderType": 4}, position,
                          {"values": values, "cumulative_borrowing_factor": 0}, {}, mismatches)
        self.assertIn("claimable_long_funding_mismatch", mismatches)

    def test_decrease_pending_impact_rounds_negative_away_from_zero(self) -> None:
        position = {"event_name": "PositionDecrease", "pre_position": {
            "sizeInUsd": 10, "sizeInTokens": 10, "pendingImpactAmount": -1,
        }, "values": {
            "sizeDeltaUsd": 5, "sizeDeltaInTokens": 5, "sizeInUsd": 5,
            "sizeInTokens": 5, "isLong": True, "indexTokenPrice.min": 1,
            "indexTokenPrice.max": 2, "uncappedBasePnlUsd": 0,
            "proportionalPendingImpactUsd": -2, "priceImpactUsd": 0,
            "executionPrice": 1,
        }}
        checks: dict[str, str] = {}
        mismatches: list[str] = []
        _compare_position_math(position, checks, mismatches)
        self.assertEqual(checks["proportional_pending_impact"], "matched")
        position["values"]["proportionalPendingImpactUsd"] = -1
        _compare_position_math(position, {}, mismatches)
        self.assertIn("proportional_pending_impact_mismatch", mismatches)


def _validation_entry(event_name: str, block_number: int, values: dict[str, object]) -> dict[str, object]:
    return {
        "event_name": event_name,
        "values": values,
        "block_number": block_number,
        "transaction_index": 0,
        "log_index": 0,
        "transaction_hash": f"0xtx-{block_number}",
    }


class AdaptiveRangeTests(unittest.TestCase):
    def test_rejected_range_is_split_without_losing_readable_blocks(self) -> None:
        calls: list[tuple[int, int]] = []
        gaps: list[RangeGap] = []

        def fetch(start: int, end: int) -> list[dict[str, int]]:
            calls.append((start, end))
            if end - start > 1:
                raise SourceError("range too large")
            return [{"block": block} for block in range(start, end + 1)]

        rows = list(adaptive_ranges(fetch, 10, 13, gaps.append, source="logs"))

        self.assertEqual(gaps, [])
        self.assertEqual([(start, end) for start, end, _ in rows], [(10, 11), (12, 13)])
        self.assertIn((10, 13), calls)

    def test_unreadable_single_block_is_an_explicit_gap(self) -> None:
        gaps: list[RangeGap] = []

        def fetch(_start: int, _end: int) -> list[dict[str, int]]:
            raise SourceError("unavailable")

        rows = list(adaptive_ranges(fetch, 42, 42, gaps.append, source="logs"))

        self.assertEqual(rows, [])
        self.assertEqual(gaps, [RangeGap("logs", 42, 42, "unavailable")])


class CollectorIntegrityTests(unittest.TestCase):
    def test_transient_layer_stale_batch_item_is_retried_individually(self) -> None:
        rpc = PublicJsonRpc("https://archive.invalid", 30, Mock())
        batch_response = [
            {"jsonrpc": "2.0", "id": 1, "result": "0xfirst"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32000, "message": "getStateObject error: layer stale"},
            },
        ]
        retry_response = {"jsonrpc": "2.0", "id": 3, "result": "0xsecond"}
        with patch.object(rpc, "_send", side_effect=[batch_response, retry_response]) as send:
            results = rpc.call_many("eth_call", [[{"data": "0x1"}], [{"data": "0x2"}]])

        self.assertEqual(results, ["0xfirst", "0xsecond"])
        self.assertEqual(send.call_count, 2)

    def test_cli_fails_before_creating_output_without_archive_rpc_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "must-not-exist"
            argv = [
                "gmx-collect",
                "--spec",
                "unused.json",
                "--output",
                str(output),
            ]
            with patch.dict(os.environ, {"GMX_ARCHIVE_RPC_URL": ""}), patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as raised:
                    main()

            self.assertFalse(output.exists())

        self.assertEqual(raised.exception.code, 2)

    def test_archive_rpc_secret_is_redacted_from_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            recording = Path(temporary_directory) / "recording"
            collector = GmxCollector(
                _spec(),
                recording,
                archive_rpc_url="https://arb-mainnet.g.alchemy.com/v2/secret-key?token=also-secret",
            )
            collector.recorder.close()
            collector.artifacts.close()
            metadata = json.loads((recording / "metadata.json").read_text())

        self.assertEqual(metadata["archive_rpc_endpoint"], "https://arb-mainnet.g.alchemy.com/v2/<redacted>")
        self.assertNotIn("secret-key", json.dumps(metadata))

    def test_checkpoint_is_recorded_at_block_before_window(self) -> None:
        checkpoint = {
            "schema": "GmxOpeningStateCheckpoint",
            "version": 1,
            "complete": True,
            "block_number": 99,
            "block_hash": "0xcanonical",
            "market_token_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "orders": {"0xold-order": {}},
            "positions": {},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            recording = Path(temporary_directory) / "recording"
            collector = GmxCollector(_spec(), recording, archive_rpc_url="https://archive.invalid/v2/key", confirmations=1)
            collector.rpc = _FakeRpc(log_block_hash="0xcanonical")
            fake_checkpoint_collector = Mock()
            fake_checkpoint_collector.capture.return_value = checkpoint
            with patch("gmx_crypto_bot.collector.OpeningCheckpointCollector", return_value=fake_checkpoint_collector):
                report = collector.collect(100, 100)
            events, _metadata = load_recording(recording)

        fake_checkpoint_collector.capture.assert_called_once_with(99)
        opening = [event for event in events if event.kind == "opening_state_checkpoint"]
        self.assertEqual(len(opening), 1)
        self.assertEqual(opening[0].block_number, 99)
        self.assertEqual(opening[0].transaction_index, -1)
        self.assertTrue(report["complete"])
        self.assertIn("0xold-order", collector._target_order_keys)

    def test_mismatched_log_block_hash_marks_recording_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            collector = GmxCollector(_spec(), Path(temporary_directory) / "recording", confirmations=1)
            collector.rpc = _FakeRpc(log_block_hash="0xreplaced")
            report = collector.collect(100, 100)

        self.assertFalse(report["complete"])
        self.assertEqual(report["reorgs"][0]["block_number"], 100)
        self.assertEqual(report["reorgs"][0]["canonical_block_hash"], "0xcanonical")

    def test_replay_events_do_not_reference_raw_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            recording = Path(temporary_directory) / "recording"
            collector = GmxCollector(_spec(), recording, confirmations=1)
            collector.rpc = _FakeRpc(log_block_hash="0xcanonical")
            collector.collect(100, 100)
            events, _ = load_recording(recording)

        self.assertTrue(all("raw_artifact" not in event.payload for event in events))

    def test_market_snapshot_excludes_unrelated_markets(self) -> None:
        snapshot = target_snapshot(
            {
                "markets": [
                    {"marketToken": "0xtarget", "name": "ETH/USD"},
                    {"marketToken": "0xother", "name": "BTC/USD"},
                ],
                "unrelated": {"value": "keep out"},
            },
            "0xtarget",
        )

        self.assertEqual(snapshot, {"markets": [{"marketToken": "0xtarget", "name": "ETH/USD"}]})

    def test_event_name_decodes_gmx_event_emitter_payload(self) -> None:
        name = b"OrderCreated"
        data = b"\0" * 32 + (96).to_bytes(32, "big") + b"\0" * 32 + len(name).to_bytes(32, "big") + name.ljust(32, b"\0")

        self.assertEqual(event_name({"data": "0x" + data.hex()}), "OrderCreated")

    def test_target_order_lifecycle_is_retained_without_raw_replay_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            collector = GmxCollector(_spec(), Path(temporary_directory) / "recording")
            logs = iter(
                [
                    DecodedEventLog("OrderCreated", {"market": collector._market_address, "key": "0xorder"}),
                    DecodedEventLog("OrderExecuted", {"key": "0xorder"}),
                ]
            )
            with patch("gmx_crypto_bot.collector.decode_event_log", side_effect=lambda _data: next(logs)):
                self.assertEqual(collector._log_scope("event_emitter", {"data": "0xcreated"}), "market")
                self.assertEqual(collector._log_scope("event_emitter", {"data": "0xexecuted"}), "order_lifecycle")
            collector.recorder.close()
            collector.artifacts.close()

    def test_rpc_redaction_preserves_non_secret_public_path(self) -> None:
        self.assertEqual(redact_rpc_endpoint("https://arb1.arbitrum.io/rpc"), "https://arb1.arbitrum.io/rpc")


def _spec() -> dict[str, object]:
    return {
        "schema": "GmxMarketSpec",
        "deployment": {"market_name": "test", "market_token_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        "anchor_block": {"rpc_url": "https://example.invalid"},
        "contracts": {
            "event_emitter": "0x0000000000000000000000000000000000000001",
            "data_store": "0x0000000000000000000000000000000000000002",
            "oracle": "0x0000000000000000000000000000000000000003",
            "order_handler": "0x0000000000000000000000000000000000000004",
            "liquidation_handler": "0x0000000000000000000000000000000000000005",
        },
        "tokens": {
            "index": {"address": "0xindex"},
            "long": {"address": "0xlong"},
            "short": {"address": "0xshort"},
        },
        "source_urls": {},
    }


class _FakeRpc:
    def __init__(self, log_block_hash: str) -> None:
        self.log_block_hash = log_block_hash

    def call(self, method: str, params: list[object]) -> object:
        if method == "eth_blockNumber":
            return "0x66"
        if method == "eth_getBlockByNumber":
            return {"hash": "0xcanonical", "timestamp": "0x1", "number": params[0]}
        if method == "eth_getLogs":
            address = params[0]["address"]
            if address.endswith("1"):
                return [
                    {
                        "blockNumber": "0x64",
                        "transactionIndex": "0x0",
                        "logIndex": "0x0",
                        "blockHash": self.log_block_hash,
                        "transactionHash": "0xtx",
                        "data": "0x" + ("0" * 64) + ("0" * 24) + ("a" * 40),
                    }
                ]
            return []
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": params[0], "gasUsed": "0x1"}
        raise AssertionError(f"unexpected RPC method: {method}")
