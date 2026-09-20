from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gmx_crypto_bot.collector import GmxCollector, RangeGap, SourceError, adaptive_ranges, event_name, target_snapshot


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
        self.last_artifact = None

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
