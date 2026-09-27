"""Verify ERC-20, native and multichain payout observations."""

from __future__ import annotations

from collections import Counter

from gmx_crypto_bot_v2.domain.constants import (
    ARBITRUM_MULTICHAIN_VAULT as MULTICHAIN_VAULT,
)
from gmx_crypto_bot_v2.domain.payments import walk_trace
from gmx_crypto_bot_v2.evidence.traces import TraceEvidenceSource
from gmx_crypto_bot_v2.models.settlement import ZERO


def verify_native(
    recording: TraceEvidenceSource | None,
    position: dict,
    receipt: dict,
    market: str,
    receiver: str,
    amount: int,
    wnt: str,
) -> str:
    if recording is None:
        return "unavailable"
    trace = recording.get(position["transaction_hash"])
    if trace is None:
        return "unavailable"
    data = trace.captured
    raw_receipt = data["receipt"]
    if (
        data["transaction_hash"] != position["transaction_hash"]
        or raw_receipt["transactionHash"] != position["transaction_hash"]
        or data["block_number"] != position["block_number"]
        or data["block_hash"] != raw_receipt["blockHash"]
        or int(raw_receipt["blockNumber"], 16) != position["block_number"]
        or raw_receipt["status"] != "0x1"
        or raw_receipt["blockHash"] != position["block_hash"]
    ):
        return "mismatch"
    frames = list(walk_trace(data["trace"]))
    native = [
        node
        for node in frames
        if node.committed
        and node.frame.get("type") == "CALL"
        and node.frame.get("from") == market
        and node.frame.get("to") == receiver
        and int(node.frame.get("value", "0x0"), 16) == amount
    ]
    withdrawals = [
        node
        for node in frames
        if node.committed
        and node.frame.get("type") == "CALL"
        and node.frame.get("from") == market
        and node.frame.get("to") == wnt
        and node.frame.get("input") == "0x2e1a7d4d" + format(amount, "064x")
    ]
    # Require a unique withdrawal and recipient CALL in the same transfer scope.
    return (
        "matched"
        if len(native) == len(withdrawals) == 1
        and (
            native[0].path[:-1] == withdrawals[0].path[:-1]
            and native[0].path > withdrawals[0].path
        )
        else "mismatch"
    )


def verify_payouts(
    expected: Counter,
    request: dict,
    position: dict,
    payouts: list,
    metadata: dict,
    recording: TraceEvidenceSource | None,
    receipt: dict,
) -> str:
    market, receiver = request["market"], request["receiver"]
    transfers = [
        e for e in payouts if e["event_name"] == "ERC20Transfer" and e["amount"] > 0
    ]
    credits = [
        e["values"]
        for e in payouts
        if e["event_name"] == "MultichainTransferIn"
        and e["values"].get("amount", 0) > 0
    ]
    if request.get("srcChainId"):
        actual = Counter(
            (e["token"], e["amount"])
            for e in transfers
            if e["from"] == market and e["to"] == MULTICHAIN_VAULT
        )
        credited = Counter(
            (v["token"], v["amount"])
            for v in credits
            if v["account"] == receiver and v["srcChainId"] == request["srcChainId"]
        )
        return (
            "matched"
            if actual == credited == expected
            and len(transfers) == sum(expected.values())
            and len(credits) == sum(expected.values())
            else "mismatch"
        )
    if credits:
        return "mismatch"
    wnt = metadata["tokens"]["index"]["address"].lower()
    actual = Counter()
    native_status = "matched"
    for event in transfers:
        if event["from"] != market:
            return "mismatch"
        if event["to"] == receiver:
            actual[(event["token"], event["amount"])] += 1
        elif (
            event["to"] == ZERO
            and event["token"] == wnt
            and request.get("shouldUnwrapNativeToken")
        ):
            actual[(wnt, event["amount"])] += 1
            native_status = verify_native(
                recording, position, receipt, market, receiver, event["amount"], wnt
            )
        else:
            return "mismatch"
    return native_status if actual == expected else "mismatch"
