from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gmx_crypto_bot.recording import JsonlRecorder, load_recording
from gmx_crypto_bot.replay import canonical_events, replay


class RecordingReplayTests(unittest.TestCase):
    def test_chain_events_replay_in_block_transaction_log_order(self) -> None:
        with self.subTest("recording"):
            with self._recorder({"chain": "arbitrum", "source": "test"}) as recorder:
                recorder.record("oracle_price", {"price": "3000"}, block_number=101, transaction_index=1, log_index=2)
                recorder.record("order_created", {"key": "0xabc"}, block_number=100, transaction_index=2, log_index=0)
                recorder.record("collector_note", {"message": "received"})
                recorder.record("order_executed", {"key": "0xabc"}, block_number=101, transaction_index=1, log_index=1)

        events, metadata = load_recording(self._path)
        ordered = canonical_events(events)

        self.assertEqual(metadata["chain"], "arbitrum")
        self.assertEqual(
            [event.kind for event in ordered],
            ["order_created", "order_executed", "oracle_price", "collector_note"],
        )

    def test_replay_digest_is_deterministic(self) -> None:
        with self._recorder({"chain": "arbitrum"}) as recorder:
            recorder.record("order_created", {"key": "0xabc"}, block_number=100, transaction_index=0, log_index=0)

        events, _ = load_recording(self._path)
        self.assertEqual(replay(events).digest, replay(events).digest)

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._path = Path(self._temporary_directory.name) / "sample"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _recorder(self, metadata: dict[str, str]) -> JsonlRecorder:
        return JsonlRecorder(self._path, metadata)
