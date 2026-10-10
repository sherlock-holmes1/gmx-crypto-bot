"""Re-derive an archive sidecar's reported values from its own raw transcript.

Every other pinned Step 4.1 artifact is replayed: the adapter recomputes each
storage key, rebuilds each call's calldata, and decodes each ABI result from the
saved `rpc_transcript` before it may use a value. The sidecar was the single
exception, yet `fixed_values.market` and `position.value` feed reject-side rules
(`market_and_collateral_token_valid`, `minimum_position_and_collateral`,
`max_open_interest`, `reserve_and_open_interest_reserve`). A reject-side
`disagreement` claims GMX and our model differ at equivalent state, so a trusted
summary field in the sidecar could manufacture that claim on its own.

This module closes that gap. It derives every storage key independently from the
pinned order, locates the matching `eth_call` in the saved transcript, decodes
the raw ABI word, and requires the rebuilt order, position, market identity,
fixed-cell inventory, fixed values, virtual inventory, and execute-order feature
flag to equal what the sidecar reports. Any missing call, ambiguous call,
changed block tag, changed digest, or changed value fails closed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gmx_crypto_bot_v2.collection.checkpoint import _decode_order, _decode_position
from gmx_crypto_bot_v2.crosscheck.archive_state import (
    ArchiveSnapshot, REQUIRED_GROUPS, _decode, aggregate_open_interest_tokens,
    execute_order_feature_cell,
    fixed_eth_usdc_increase_cells, market_cell, position_key, virtual_inventory_tokens_cell,
    virtual_token_id_cell, ZERO_ID,
)
from gmx_crypto_bot_v2.crosscheck.router_preflight import (
    _ADDRESS_FIELDS, _ARRAY_FIELDS, _BOOL_FIELDS, _FIELDS, _field_key, _hex_bytes,
    _key, _selector, latest_key,
)


REPLAYED_SCHEMA = "GmxStep41ArchiveSidecarTranscriptReplay"


class _Transcript:
    """Index the sidecar's saved read-only calls by target and calldata."""

    def __init__(self, rows: Any, block: int, block_hash: str, chain_id: int):
        if not isinstance(rows, list) or not rows or \
                any(not isinstance(row, dict) for row in rows):
            raise ValueError("sidecar transcript is missing or malformed")
        self.block_tag = hex(block)
        self.calls: dict[tuple[str, str], str] = {}
        headers = chains = 0
        for row in rows:
            method, params = row.get("method"), row.get("params")
            response = row.get("response")
            if not isinstance(response, dict) or "result" not in response:
                # A redacted provider error has no result; it proves nothing and
                # must never silently stand in for a value.
                continue
            result = response["result"]
            if method == "eth_chainId":
                if params != [] or int(result, 16) != chain_id:
                    raise ValueError("sidecar transcript chain mismatch")
                chains += 1
            elif method == "eth_getBlockByNumber":
                if params != [self.block_tag, False] or not isinstance(result, dict) or \
                        int(result.get("number", "-1"), 16) != block or \
                        str(result.get("hash", "")).lower() != block_hash:
                    raise ValueError("sidecar transcript block mismatch")
                headers += 1
            elif method == "eth_call":
                if not isinstance(params, list) or len(params) != 2 or \
                        not isinstance(params[0], dict) or params[1] != self.block_tag or \
                        set(params[0]) != {"to", "data"} or not isinstance(result, str):
                    raise ValueError("sidecar transcript call shape mismatch")
                identity = (str(params[0]["to"]).lower(), str(params[0]["data"]).lower())
                if self.calls.setdefault(identity, result) != result:
                    raise ValueError("sidecar transcript repeats a call with two results")
            elif method != "eth_getCode":
                raise ValueError("sidecar transcript holds a non-read-only method")
        if not chains or not headers:
            raise ValueError("sidecar transcript lacks chain or block identity")

    def read(self, to: str, data: bytes) -> str:
        identity = (str(to).lower(), ("0x" + data.hex()).lower())
        if identity not in self.calls:
            raise ValueError("sidecar transcript lacks a required call")
        return self.calls[identity]

    def word(self, to: str, data: bytes) -> bytes:
        return _hex_bytes(self.read(to, data), 32)


def _array(raw: str, size: int) -> list[str]:
    data = bytes.fromhex(raw[2:])
    if len(data) < 64 or int.from_bytes(data[:32], "big") != 32:
        raise ValueError("malformed sidecar array result")
    count = int.from_bytes(data[32:64], "big")
    if count > 1024 or len(data) != 64 + count * 32:
        raise ValueError("malformed sidecar array length")
    return ["0x" + data[64 + i * 32 + (32 - size):96 + i * 32].hex() for i in range(count)]


def _replay_order(transcript: _Transcript, datastore: str, order_key: str) -> dict[str, Any]:
    key_bytes = _hex_bytes(order_key, 32)
    order: dict[str, Any] = {}
    for name, label in _FIELDS:
        field_key = _field_key(key_bytes, label)
        if name in _ARRAY_FIELDS:
            signature, size = _ARRAY_FIELDS[name]
            order[name] = _array(transcript.read(
                datastore, _selector(signature) + field_key), size)
        elif name in _ADDRESS_FIELDS:
            word = transcript.word(datastore, _selector("getAddress(bytes32)") + field_key)
            if int.from_bytes(word, "big") >= 2**160:
                raise ValueError("malformed sidecar order address")
            order[name] = "0x" + word[-20:].hex()
        elif name in _BOOL_FIELDS:
            value = int.from_bytes(transcript.word(
                datastore, _selector("getBool(bytes32)") + field_key), "big")
            if value not in (0, 1):
                raise ValueError("malformed sidecar order boolean")
            order[name] = bool(value)
        else:
            order[name] = int.from_bytes(transcript.word(
                datastore, _selector("getUint(bytes32)") + field_key), "big")
    return order


def _reader_struct(transcript: _Transcript, reader: str, datastore: str,
                   signature: str, item_key: str) -> str:
    data = (_selector(signature) + _hex_bytes(datastore, 20).rjust(32, b"\0") +
            _hex_bytes(item_key, 32))
    return transcript.read(reader, data)


def verify_sidecar_transcript_values(sidecar: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the sidecar's reported values from its raw transcript, or fail.

    Returns a small replay summary. Raises `ValueError` on any mismatch; no
    caller may continue with a sidecar whose values it could not re-derive.
    """
    if sidecar.get("schema") != "GmxStep41ArchiveSidecar" or sidecar.get("version") != 1:
        raise ValueError("sidecar replay requires a version 1 archive sidecar")
    if sidecar.get("status") != "partial_archive_snapshot":
        raise ValueError("sidecar replay requires a collected archive snapshot")
    rows = sidecar.get("rpc_transcript")
    digest = "0x" + hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if sidecar.get("rpc_transcript_sha256") != digest:
        raise ValueError("sidecar transcript digest mismatch")
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    block = pin["number"]
    block_hash = str(pin["recorded_hash"]).lower()
    if str(pin.get("archive_hash", "")).lower() != block_hash or \
            str(pin.get("hash_after", "")).lower() != block_hash:
        raise ValueError("sidecar pin hashes disagree")
    datastore, reader = deployment["datastore"], deployment["reader"]
    transcript = _Transcript(rows, block, block_hash, deployment["chain_id"])

    order_key = str(sidecar["source"]["order_key"]).lower()
    nonce = int.from_bytes(transcript.word(
        datastore, _selector("getUint(bytes32)") + _key("NONCE")), "big")
    pending = int.from_bytes(transcript.word(
        datastore, _selector("containsBytes32(bytes32,bytes32)") +
        _key("ORDER_LIST") + _hex_bytes(order_key, 32)), "big")
    if pending not in (0, 1):
        raise ValueError("malformed sidecar pending membership")
    if sidecar.get("router_gate") != {
            "nonce": nonce, "derived_latest_key": latest_key(datastore, nonce),
            "latest": latest_key(datastore, nonce) == order_key, "pending": bool(pending)}:
        raise ValueError("sidecar router gate differs from its transcript")

    order = _replay_order(transcript, datastore, order_key)
    if order != sidecar.get("pinned_order"):
        raise ValueError("sidecar pinned order differs from its transcript")

    # The Reader order is a second, independent read of the same request.
    if _decode_order(_reader_struct(transcript, reader, datastore,
                                    "getOrder(address,bytes32)", order_key)) != order:
        raise ValueError("sidecar Reader order differs from its DataStore order")

    derived_position_key = position_key(order["account"], order["market"],
                                        order["initialCollateralToken"], order["isLong"])
    position = _decode_position(_reader_struct(
        transcript, reader, datastore, "getPosition(address,bytes32)", derived_position_key))
    if sidecar.get("position") != {"key": derived_position_key, "value": position}:
        raise ValueError("sidecar position differs from its transcript")

    market = {name: _decode(transcript.read(
        datastore, _selector("getAddress(bytes32)") +
        _hex_bytes(market_cell(name, order["market"]).storage_key, 32)), "Address")
        for name in ("MARKET_TOKEN", "INDEX_TOKEN", "LONG_TOKEN", "SHORT_TOKEN")}
    if market["MARKET_TOKEN"].lower() != order["market"].lower():
        raise ValueError("sidecar market token is not the order's market")

    cells = fixed_eth_usdc_increase_cells(
        order, index_token=market["INDEX_TOKEN"], long_token=market["LONG_TOKEN"],
        short_token=market["SHORT_TOKEN"])
    if [vars(cell) for cell in cells] != sidecar.get("fixed_cells"):
        raise ValueError("sidecar fixed cell inventory differs from the pinned order")
    values: dict[str, dict[str, Any]] = {group: {} for group in REQUIRED_GROUPS}
    for cell in cells:
        values[cell.group][cell.name] = _decode(transcript.read(
            datastore, _selector(f"get{cell.kind}(bytes32)") +
            _hex_bytes(cell.storage_key, 32)), cell.kind)
    if values != sidecar.get("fixed_values"):
        raise ValueError("sidecar fixed values differ from its transcript")

    identity_cell = virtual_token_id_cell(market["INDEX_TOKEN"])
    virtual_id = _decode(transcript.read(
        datastore, _selector("getBytes32(bytes32)") +
        _hex_bytes(identity_cell.storage_key, 32)), "Bytes32")
    virtual_tokens = None
    if virtual_id != ZERO_ID:
        inventory_cell = virtual_inventory_tokens_cell(virtual_id)
        virtual_tokens = _decode(transcript.read(
            datastore, _selector("getInt(bytes32)") +
            _hex_bytes(inventory_cell.storage_key, 32)), "Int")
    open_interest = aggregate_open_interest_tokens(
        ArchiveSnapshot(deployment["chain_id"], block, block_hash, datastore,
                        deployment["datastore_code_hash"], cells, values),
        long_token=market["LONG_TOKEN"], short_token=market["SHORT_TOKEN"])
    if sidecar.get("virtual_inventory") != {"token_id": virtual_id, "tokens": virtual_tokens,
                                            "open_interest_tokens": open_interest}:
        raise ValueError("sidecar virtual inventory differs from its transcript")

    feature = sidecar.get("feature_flag")
    if deployment.get("order_handler"):
        cell = execute_order_feature_cell(deployment["order_handler"], order["orderType"])
        disabled = _decode(transcript.read(
            datastore, _selector("getBool(bytes32)") +
            _hex_bytes(cell.storage_key, 32)), "Bool")
        if feature != {"cell": vars(cell), "disabled": disabled}:
            raise ValueError("sidecar feature flag differs from its transcript")
    elif feature is not None:
        raise ValueError("sidecar reports a feature flag without a pinned order handler")

    return {"schema": REPLAYED_SCHEMA, "version": 1,
            "rpc_transcript_sha256": digest,
            "replayed_calls": len(transcript.calls),
            "pin_block": block, "pin_block_hash": block_hash,
            "order_key": order_key, "position_key": derived_position_key,
            "router_gate_nonce": nonce,
            "market": market,
            "sidecar_values_rederived_from_raw_transcript": True}
