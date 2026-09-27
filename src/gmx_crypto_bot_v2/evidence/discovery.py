"""Discover collection targets from recorded facts, without economic checks."""

from collections import defaultdict

from gmx_crypto_bot_v2.domain.checkpoint import normalize_recorded_order_checkpoint
from gmx_crypto_bot_v2.domain.constants import TERMINAL_EVENTS
from gmx_crypto_bot_v2.domain.entries import _decode_recorded_log, decoded_log
from gmx_crypto_bot_v2.domain.evidence import (
    BlockReference,
    CollectionRequirements,
    TraceRequirement,
)
from gmx_crypto_bot_v2.evidence.repository import event_rows, raw_logs


def discover(recording) -> CollectionRequirements:
    orders = {}
    for event in event_rows(
        recording, kinds={"opening_state_checkpoint", "gmx_market_log"}
    ):
        if event["kind"] == "opening_state_checkpoint":
            for key, order in event["payload"].get("orders", {}).items():
                values = order.get("opening_checkpoint")
                if isinstance(values, dict):
                    orders[key.lower()] = normalize_recorded_order_checkpoint(values)
        elif event["payload"].get("event_name") == "OrderCreated":
            values = _decode_recorded_log(event).values
            orders[values["key"].lower()] = dict(values)
    traders, markets, receivers = set(), set(), set()
    trace_blocks, trace_reasons = {}, defaultdict(set)
    events = TERMINAL_EVENTS | {
        "OrderUpdated",
        "SwapInfo",
        "SwapFeesCollected",
        "PositionFeesCollected",
    }
    for log in raw_logs(recording, events):
        decoded = decoded_log(log)
        values, name = decoded.values, decoded.event_name
        key = values.get(
            "key", values.get("orderKey", values.get("tradeKey", ""))
        ).lower()
        if key not in orders:
            continue
        request = orders[key]
        if name == "OrderUpdated":
            request.update(values)
        if name == "OrderExecuted":
            account = request.get("account")
            if account:
                traders.add(account.lower())
        if name in TERMINAL_EVENTS:
            transaction = log["transactionHash"].lower()
            anchor = BlockReference(int(log["blockNumber"], 16), log["blockHash"])
            if transaction in trace_blocks and trace_blocks[transaction] != anchor:
                raise ValueError("conflicting terminal transaction block")
            # Trace every terminal transaction: zero-fee liquidation payouts need
            # the same provenance even when there is no payExecutionFee call.
            trace_blocks[transaction] = anchor
            trace_reasons[transaction].add("terminal_execution")
            if request.get("shouldUnwrapNativeToken"):
                trace_reasons[transaction].add("native_payout")
        if name == "SwapInfo" and values.get("market"):
            markets.add(values["market"].lower())
        receiver = values.get("uiFeeReceiver")
        if receiver:
            receivers.add(receiver.lower())
    for request in orders.values():
        markets.update(m.lower() for m in request.get("swapPath", []))
        if request.get("uiFeeReceiver"):
            receivers.add(request["uiFeeReceiver"].lower())
    return CollectionRequirements(
        frozenset(orders),
        frozenset(traders),
        frozenset(markets),
        frozenset(receivers),
        tuple(
            TraceRequirement(tx, trace_blocks[tx], frozenset(trace_reasons[tx]))
            for tx in sorted(trace_blocks)
        ),
    )
