from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gmx_crypto_bot_v2.recording import JsonlRecorder, load_recording
from gmx_crypto_bot_v2.replay import canonical_events, replay
from gmx_crypto_bot_v2.event_decoder import DecodedEventLog
from gmx_crypto_bot_v2.state import build_state, report_from_state


class RecordingReplayTests(unittest.TestCase):
    def test_chain_events_replay_in_block_transaction_log_order(self) -> None:
        with self.subTest("recording"):
            with self._recorder({"chain": "arbitrum", "source": "test"}) as recorder:
                recorder.record(
                    "oracle_price",
                    {"price": "3000"},
                    block_number=101,
                    transaction_index=1,
                    log_index=2,
                )
                recorder.record(
                    "order_created",
                    {"key": "0xabc"},
                    block_number=100,
                    transaction_index=2,
                    log_index=0,
                )
                recorder.record("collector_note", {"message": "received"})
                recorder.record(
                    "order_executed",
                    {"key": "0xabc"},
                    block_number=101,
                    transaction_index=1,
                    log_index=1,
                )

        events, metadata = load_recording(self._path)
        ordered = canonical_events(events)

        self.assertEqual(metadata["chain"], "arbitrum")
        self.assertEqual(
            [event.kind for event in ordered],
            ["order_created", "order_executed", "oracle_price", "collector_note"],
        )

    def test_replay_digest_is_deterministic(self) -> None:
        with self._recorder({"chain": "arbitrum"}) as recorder:
            recorder.record(
                "order_created",
                {"key": "0xabc"},
                block_number=100,
                transaction_index=0,
                log_index=0,
            )

        events, _ = load_recording(self._path)
        self.assertEqual(replay(events).digest, replay(events).digest)

    def test_replay_reconstructs_observable_state_and_marks_gaps_incomplete(
        self,
    ) -> None:
        metadata = {
            "market": {"market_token_address": "0xmarket"},
            "tokens": {"index": {"address": "0xindex"}},
            "opening_state_checkpoint": {"block_number": 1},
        }
        events = [
            self._event("oracle", 1),
            self._event("created", 2),
            self._event("interest", 3),
            self._event("executed", 4),
            self._event("gap", 5, kind="data_gap"),
        ]
        decoded = {
            "oracle": DecodedEventLog(
                "OraclePriceUpdate",
                {"token": "0xindex", "minPrice": 100, "maxPrice": 101},
            ),
            "created": DecodedEventLog(
                "OrderCreated", {"key": "0xorder", "market": "0xmarket"}
            ),
            "interest": DecodedEventLog(
                "OpenInterestUpdated",
                {
                    "market": "0xmarket",
                    "collateralToken": "0xusdc",
                    "isLong": True,
                    "nextValue": 50,
                },
            ),
            "executed": DecodedEventLog("OrderExecuted", {"key": "0xorder"}),
        }
        with patch(
            "gmx_crypto_bot_v2.reconstruction.observed.decode_event_log",
            side_effect=lambda data: decoded[data],
        ):
            state = build_state(events, metadata)

        report = report_from_state(state)
        self.assertFalse(report["complete"])
        self.assertEqual(report["data_quality"]["gaps"], 1)
        self.assertEqual(report["orders"]["terminal_outcomes"], {"OrderExecuted": 1})
        self.assertEqual(report["orders"]["request_to_terminal_blocks"]["mean"], 2)
        self.assertEqual(report["market"]["open_interest_usd"]["long"], 50)

    def test_replay_loads_embedded_opening_checkpoint(self) -> None:
        from gmx_crypto_bot_v2.recording import RecordedEvent

        metadata = {
            "market": {"market_token_address": "0xmarket"},
            "tokens": {"index": {"address": "0xindex"}},
        }
        checkpoint = RecordedEvent(
            seq=1,
            kind="opening_state_checkpoint",
            received_at="test",
            received_monotonic_ns=1,
            block_number=9,
            transaction_index=-1,
            log_index=-1,
            payload={
                "block_number": 9,
                "market_token_address": "0xmarket",
                "complete": True,
                "orders": {
                    "0xexisting": {
                        "terminal": None,
                        "created_coordinate": [9, -1, -1, -1],
                    }
                },
                "positions": {"0xposition": {"sizeInUsd": 100}},
            },
        )

        state = build_state([checkpoint], metadata)

        self.assertTrue(state.complete)
        self.assertTrue(state.opening_checkpoint_present)
        self.assertIn("0xexisting", state.orders)
        self.assertIn("0xposition", state.positions)

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._path = Path(self._temporary_directory.name) / "sample"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _recorder(self, metadata: dict[str, str]) -> JsonlRecorder:
        return JsonlRecorder(self._path, metadata)

    @staticmethod
    def _event(data: str, block_number: int, *, kind: str = "gmx_market_log"):
        from gmx_crypto_bot_v2.recording import RecordedEvent

        return RecordedEvent(
            seq=block_number,
            kind=kind,
            received_at="test",
            received_monotonic_ns=block_number,
            block_number=block_number,
            transaction_index=0,
            log_index=0,
            payload={"log": {"data": data}},
        )
