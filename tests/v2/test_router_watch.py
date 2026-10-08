"""Bounded watcher tests using a real recorded EventEmitter log shape."""

import json
import unittest
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.router_watch import watch_order_creations
from gmx_crypto_bot_v2.domain.entries import decoded_log


RECORDING = Path(__file__).resolve().parents[2] / "recordings/eth-usdc-v2-sep-20-sep-27"


def _creation_log():
    with (RECORDING / "events.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["kind"] == "gmx_market_log" and row["payload"].get("event_name") == "OrderCreated":
                return row["payload"]["log"]
    raise AssertionError("missing fixture log")


class Rpc:
    def __init__(self, logs):
        self.logs = logs
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        return {"result": self.logs}


class RouterWatchTests(unittest.TestCase):
    def test_real_creation_shape_and_coordinates(self):
        log = _creation_log()
        rpc = Rpc([log])
        block = int(log["blockNumber"], 16)
        market = decoded_log(log).values["market"]
        rows = watch_order_creations(rpc, log["address"], block, block, market)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "watch")
        self.assertEqual(rows[0]["creation"]["block_number"], block)
        self.assertEqual(rows[0]["creation"]["transaction_hash"], log["transactionHash"].lower())
        self.assertEqual(rows[0]["order_key"], "0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c")
        self.assertEqual(rpc.calls[0][0], "eth_getLogs")

    def test_watch_rejects_unbounded_range(self):
        with self.assertRaisesRegex(ValueError, "bounded"):
            watch_order_creations(Rpc([]), "0x" + "11" * 20, 1, 9000, "0x" + "22" * 20)

    def test_market_filter_and_out_of_range_rejection(self):
        log = _creation_log()
        block = int(log["blockNumber"], 16)
        wrong_market = "0x" + "ff" * 20
        self.assertEqual(watch_order_creations(Rpc([log]), log["address"], block, block,
                                               wrong_market), [])
        late = dict(log, blockNumber=hex(block + 1))
        with self.assertRaisesRegex(ValueError, "outside requested"):
            watch_order_creations(Rpc([late]), log["address"], block, block,
                                  decoded_log(log).values["market"])

    def test_same_block_terminal_skips(self):
        log = _creation_log()
        block = int(log["blockNumber"], 16)
        terminal = dict(log, _decoded={"event_name": "OrderExecuted", "values": {
            "key": decoded_log(log).values["key"]}})
        rows = watch_order_creations(Rpc([log, terminal]), log["address"], block, block,
                                     decoded_log(log).values["market"])
        self.assertIn("same_block_terminal", rows[0]["selection_skip_reasons"])
