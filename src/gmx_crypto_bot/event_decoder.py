"""Minimal ABI decoder for GMX EventEmitter EventLogData events."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class EventDecodeError(ValueError):
    """An EventEmitter log does not match the GMX EventLogData ABI."""


@dataclass(frozen=True)
class DecodedEventLog:
    event_name: str
    values: dict[str, Any]


_ITEM_TYPES = ("address", "uint", "int", "bool", "bytes32", "bytes", "string")


def event_name_from_data(data: str) -> str | None:
    """Read EventEmitter's first dynamic string without decoding the full payload."""
    try:
        encoded = _hex_bytes(data)
        return _decode_string(encoded, _offset(encoded, 32))
    except EventDecodeError:
        return None


def decode_event_log(data: str) -> DecodedEventLog:
    """Decode a GMX EventLog, EventLog1, or EventLog2 non-indexed payload."""
    encoded = _hex_bytes(data)
    event_name = _decode_string(encoded, _offset(encoded, 32))
    event_data_base = _offset(encoded, 64)
    values: dict[str, Any] = {}
    for index, item_type in enumerate(_ITEM_TYPES):
        category_base = event_data_base + _offset(encoded, event_data_base + index * 32)
        for key, value in _decode_items(encoded, category_base, item_type).items():
            if key in values:
                raise EventDecodeError(f"duplicate GMX event key: {key}")
            values[key] = value
    return DecodedEventLog(event_name=event_name, values=values)


def _decode_items(encoded: bytes, category_base: int, item_type: str) -> dict[str, Any]:
    items_base = category_base + _offset(encoded, category_base)
    length = _word(encoded, items_base)
    head_base = items_base + 32
    values: dict[str, Any] = {}
    for index in range(length):
        item_base = head_base + _offset(encoded, head_base + index * 32)
        key = _decode_string(encoded, item_base + _offset(encoded, item_base))
        values[key] = _decode_item_value(encoded, item_base, item_type)
    return values


def _decode_item_value(encoded: bytes, item_base: int, item_type: str) -> Any:
    value_offset = item_base + 32
    value = _word(encoded, value_offset)
    if item_type == "address":
        return "0x" + value.to_bytes(32, "big")[-20:].hex()
    if item_type == "uint":
        return value
    if item_type == "int":
        return value - (1 << 256) if value >= 1 << 255 else value
    if item_type == "bool":
        if value not in (0, 1):
            raise EventDecodeError(f"invalid bool value: {value}")
        return bool(value)
    if item_type == "bytes32":
        return "0x" + value.to_bytes(32, "big").hex()
    if item_type == "bytes":
        return "0x" + _decode_dynamic_bytes(encoded, item_base + value).hex()
    if item_type == "string":
        return _decode_string(encoded, item_base + value)
    raise EventDecodeError(f"unsupported GMX item type: {item_type}")


def _hex_bytes(value: str) -> bytes:
    try:
        return bytes.fromhex(value.removeprefix("0x"))
    except ValueError as error:
        raise EventDecodeError("log data is not hexadecimal") from error


def _offset(encoded: bytes, position: int) -> int:
    return _word(encoded, position)


def _word(encoded: bytes, position: int) -> int:
    if position < 0 or position + 32 > len(encoded):
        raise EventDecodeError("ABI word points outside log data")
    return int.from_bytes(encoded[position : position + 32], "big")


def _decode_string(encoded: bytes, position: int) -> str:
    try:
        return _decode_dynamic_bytes(encoded, position).decode("utf-8")
    except UnicodeDecodeError as error:
        raise EventDecodeError("ABI string is not UTF-8") from error


def _decode_dynamic_bytes(encoded: bytes, position: int) -> bytes:
    length = _word(encoded, position)
    end = position + 32 + length
    if end > len(encoded):
        raise EventDecodeError("ABI dynamic value points outside log data")
    return encoded[position + 32 : end]
