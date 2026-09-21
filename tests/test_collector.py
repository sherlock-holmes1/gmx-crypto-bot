from __future__ import annotations

import sys
import tempfile
import unittest
import base64
import gzip
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gmx_crypto_bot.collector import GmxCollector, RangeGap, SourceError, adaptive_ranges, event_name, target_snapshot
from gmx_crypto_bot.artifacts import RawArtifactStore
from gmx_crypto_bot.recording import load_recording
from gmx_crypto_bot.validator import _validate_order


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
    def test_auto_updated_size_is_compared_to_observed_execution(self) -> None:
        request = _validation_entry("OrderCreated", 1, {"account": "0xaccount", "sizeDeltaUsd": 100, "acceptablePrice": 0})
        lifecycle = [
            _validation_entry("OrderSizeDeltaAutoUpdated", 2, {"nextSizeDeltaUsd": 75}),
            _validation_entry("OrderExecuted", 3, {"account": "0xaccount"}),
        ]
        observed = [_validation_entry("PositionDecrease", 3, {"sizeDeltaUsd": 75, "executionPrice": 100})]
        receipts = {"0xtx-3": {"payload": {"status": "0x1"}}}

        result = _validate_order("0xorder", request, lifecycle, observed, receipts)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["final_request"]["sizeDeltaUsd"], 75)


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
