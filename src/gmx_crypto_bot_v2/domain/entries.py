"""Canonical decoded event entries and coordinate helpers."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.domain.events import (
    DecodedEventLog,
    EventDecodeError,
    decode_event_log,
)


def _decode_recorded_log(event: dict[str, Any]) -> DecodedEventLog | None:
    try:
        return (
            DecodedEventLog(**event["_decoded"])
            if "_decoded" in event
            else decoded_log(event["payload"]["log"])
        )
    except (EventDecodeError, KeyError):
        return None


def _event_entry(event: dict[str, Any], decoded: DecodedEventLog) -> dict[str, Any]:
    log = event["payload"]["log"]
    return {
        "event_name": decoded.event_name,
        "values": decoded.values,
        "block_number": event["block_number"],
        "transaction_index": event["transaction_index"],
        "log_index": event["log_index"],
        "transaction_hash": log["transactionHash"].lower(),
        "block_hash": log.get("blockHash"),
    }


def _event_entry_from_log(
    log: dict[str, Any], decoded: DecodedEventLog
) -> dict[str, Any]:
    return {
        "event_name": decoded.event_name,
        "values": decoded.values,
        "block_number": int(log["blockNumber"], 16),
        "transaction_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "transaction_hash": log["transactionHash"].lower(),
        "block_hash": log.get("blockHash"),
    }


def _order_key(decoded: DecodedEventLog) -> str | None:
    key = decoded.values.get("key")
    return key.lower() if isinstance(key, str) else None


def _coordinate(entry: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(entry["block_number"]),
        int(entry["transaction_index"]),
        int(entry["log_index"]),
    )


def decoded_log(log):
    return (
        DecodedEventLog(**log["_decoded"])
        if "_decoded" in log
        else decode_event_log(log["data"])
    )
