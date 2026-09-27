"""Select target-market fields from public observations."""

from __future__ import annotations

from typing import Any


def contains_address(log: dict[str, Any], address: str) -> bool:
    """Check ABI-padded topics and data without relying on an event-specific decoder."""
    normalized = address.removeprefix("0x").lower()
    padded = "0x" + "0" * 24 + normalized
    topics = [value.lower() for value in log.get("topics", [])]
    data = log.get("data", "").lower()
    return any(padded in value for value in topics) or padded[2:] in data


def event_name(log: dict[str, Any]) -> str | None:
    """Decode the first dynamic string in GMX EventEmitter EventLog payloads."""
    try:
        encoded = bytes.fromhex(log["data"].removeprefix("0x"))
        if len(encoded) < 96:
            return None
        offset = int.from_bytes(encoded[32:64], "big")
        if offset + 32 > len(encoded):
            return None
        size = int.from_bytes(encoded[offset : offset + 32], "big")
        value = encoded[offset + 32 : offset + 32 + size]
        decoded = value.decode("ascii")
        return decoded if decoded.isidentifier() else None
    except (KeyError, UnicodeDecodeError, ValueError):
        return None


def target_snapshot(payload: Any, market_address: str) -> Any:
    """Extract target-market objects from a public API response without mutating raw evidence."""
    if isinstance(payload, list):
        matches = [item for item in payload if contains_value(item, market_address)]
        return matches
    if isinstance(payload, dict):
        if any(
            isinstance(value, str) and value.lower() == market_address.lower()
            for key, value in payload.items()
            if key.lower()
            in {
                "market",
                "marketaddress",
                "market_address",
                "markettoken",
                "market_token",
            }
        ):
            return payload
        matches = {
            key: target_snapshot(value, market_address)
            for key, value in payload.items()
            if isinstance(value, (dict, list))
        }
        return {key: value for key, value in matches.items() if value not in ({}, [])}
    return None


def contains_value(value: Any, address: str) -> bool:
    if isinstance(value, str):
        return value.lower() == address.lower()
    if isinstance(value, list):
        return any(contains_value(item, address) for item in value)
    if isinstance(value, dict):
        return any(contains_value(item, address) for item in value.values())
    return False
