"""Capture the pinned DataStore and delegated-contract transcripts for one pin.

Stage 3c introduced six pinned DataStore transcripts and two delegated-contract
proofs, but captured them by hand. Stage 3d must repin every one of them at the
prestate-proved pre-execution boundary, so the capture becomes a tool: each
record is produced by read-only RPC with its storage keys derived locally, and
each is immediately replayed through the adapter's own `verify_*` function
before it is written. A record that its verifier cannot rebuild is never saved.

Nothing here signs, submits, or mutates. Every call is `eth_chainId`,
`eth_getBlockByNumber`, `eth_call`, or `eth_getCode`, and the block hash is read
before and after each capture so a changed block fails the capture closed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gmx_crypto_bot_v2.domain.configuration import ConfigKey
from gmx_crypto_bot_v2.domain.keys import config_market_data, keccak256
from gmx_crypto_bot_v2.domain.swap_keys import key as storage_key
from gmx_crypto_bot_v2.crosscheck.router_preflight import RawRpc, _hex_bytes, _selector


READ_ONLY_METHODS = frozenset({"eth_chainId", "eth_getBlockByNumber", "eth_call",
                               "eth_getCode"})


class TranscriptRpc:
    """Record exact read-only requests and raw responses, without an endpoint."""

    def __init__(self, inner: RawRpc):
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, params: list[Any]) -> Any:
        if method not in READ_ONLY_METHODS:
            raise ValueError("pinned capture permits read-only methods only")
        envelope = self.inner.request(method, params)
        if not isinstance(envelope, dict):
            raise OSError(f"{method} returned a malformed envelope")
        if "error" in envelope or "result" not in envelope:
            # A provider error body can echo a credential-bearing endpoint.
            self.calls.append({"method": method, "params": params,
                               "response": {"error": "redacted_provider_error"}})
            raise OSError(f"{method} failed")
        self.calls.append({"method": method, "params": params, "response": envelope})
        return envelope["result"]


class _PinnedReader:
    """Read DataStore cells and contract code at one verified block hash."""

    def __init__(self, rpc: RawRpc, *, chain_id: int, block: int, block_hash: str):
        self.traced = TranscriptRpc(rpc)
        self.chain_id = chain_id
        self.block = block
        self.block_hash = "0x" + _hex_bytes(block_hash, 32).hex()
        self.tag = hex(block)

    def open(self) -> dict[str, Any]:
        if int(self.traced.request("eth_chainId", []), 16) != self.chain_id:
            raise ValueError("pinned capture chain mismatch")
        return self._header()

    def _header(self) -> dict[str, Any]:
        header = self.traced.request("eth_getBlockByNumber", [self.tag, False])
        if not isinstance(header, dict) or int(header.get("number", "-1"), 16) != self.block or \
                str(header.get("hash", "")).lower() != self.block_hash:
            raise ValueError("pinned capture block identity mismatch")
        return header

    def close(self) -> None:
        self._header()

    def _word(self, to: str, signature: str, key: str) -> int:
        data = "0x" + (_selector(signature) + _hex_bytes(key, 32)).hex()
        raw = self.traced.request("eth_call", [{"to": to, "data": data}, self.tag])
        return int.from_bytes(_hex_bytes(raw, 32), "big")

    def uint(self, datastore: str, key: str) -> int:
        return self._word(datastore, "getUint(bytes32)", key)

    def boolean(self, datastore: str, key: str) -> bool:
        value = self._word(datastore, "getBool(bytes32)", key)
        if value not in (0, 1):
            raise ValueError("pinned capture read a malformed boolean cell")
        return bool(value)

    def pointer(self, contract: str, signature: str) -> str:
        """Read a zero-argument address getter, deriving its selector locally."""
        data = "0x" + _selector(signature).hex()
        raw = self.traced.request("eth_call", [{"to": contract, "data": data}, self.tag])
        value = int.from_bytes(_hex_bytes(raw, 32), "big")
        if value >= 2**160:
            raise ValueError("pinned capture read a malformed address pointer")
        return "0x" + f"{value:040x}"

    def code(self, address: str) -> bytes:
        raw = self.traced.request("eth_getCode", [address, self.tag])
        if not isinstance(raw, str) or not raw.startswith("0x") or raw == "0x":
            raise ValueError("pinned capture found no code at the pin")
        return bytes.fromhex(raw[2:])


def _base(schema: str, reader: _PinnedReader, datastore: str) -> dict[str, Any]:
    return {"schema": schema, "version": 1, "chain_id": reader.chain_id,
            "block_number": reader.block, "block_hash": reader.block_hash,
            "datastore": datastore}


def capture_decision_config(rpc: RawRpc, *, chain_id: int, datastore: str, market: str,
                            block: int, block_hash: str) -> dict[str, Any]:
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    reader.open()
    keys = {"request_expiration_time": ConfigKey("REQUEST_EXPIRATION_TIME").storage_key,
            "is_market_disabled": ConfigKey("IS_MARKET_DISABLED",
                                            config_market_data(market)).storage_key}
    values = {"request_expiration_time": reader.uint(datastore, keys["request_expiration_time"]),
              "is_market_disabled": reader.boolean(datastore, keys["is_market_disabled"])}
    reader.close()
    return {**_base("GmxStep41PinnedDecisionConfig", reader, datastore), "market": market,
            "keys": keys, "values": values, "rpc_transcript": reader.traced.calls}


def capture_risk_cells(rpc: RawRpc, *, chain_id: int, datastore: str, market_token: str,
                       long_token: str, short_token: str, block: int,
                       block_hash: str) -> dict[str, Any]:
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    reader.open()
    keys = {
        "min_position_size_usd": storage_key("MIN_POSITION_SIZE_USD"),
        "reserve_factor_long": storage_key("RESERVE_FACTOR", market_token, True),
        "open_interest_reserve_factor_long": storage_key(
            "OPEN_INTEREST_RESERVE_FACTOR", market_token, True),
        "long_oi_usd_weth_collateral": storage_key(
            "OPEN_INTEREST", market_token, long_token, True),
        "long_oi_usd_usdc_collateral": storage_key(
            "OPEN_INTEREST", market_token, short_token, True),
        "min_collateral_usd": storage_key("MIN_COLLATERAL_USD"),
        "min_collateral_factor": storage_key("MIN_COLLATERAL_FACTOR", market_token),
        "min_collateral_factor_for_oi_multiplier_long": storage_key(
            "MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER", market_token, True),
        "max_open_interest_long": storage_key("MAX_OPEN_INTEREST", market_token, True),
    }
    values = {name: reader.uint(datastore, key) for name, key in keys.items()}
    reader.close()
    return {**_base("GmxStep41PinnedRiskCells", reader, datastore),
            "keys": keys, "values": values, "rpc_transcript": reader.traced.calls}


def capture_balance_inputs(rpc: RawRpc, *, chain_id: int, datastore: str, market_token: str,
                           long_token: str, short_token: str, block: int,
                           block_hash: str) -> dict[str, Any]:
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    reader.open()
    keys = {
        "use_oi_tokens_for_balance": storage_key("USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE"),
        "short_oi_usd_weth_collateral": storage_key(
            "OPEN_INTEREST", market_token, long_token, False),
        "short_oi_usd_usdc_collateral": storage_key(
            "OPEN_INTEREST", market_token, short_token, False),
    }
    values: dict[str, Any] = {
        "use_oi_tokens_for_balance": reader.boolean(datastore, keys["use_oi_tokens_for_balance"])}
    for name in ("short_oi_usd_weth_collateral", "short_oi_usd_usdc_collateral"):
        values[name] = reader.uint(datastore, keys[name])
    reader.close()
    return {**_base("GmxStep41PinnedBalanceInputs", reader, datastore),
            "keys": keys, "values": values, "rpc_transcript": reader.traced.calls}


def capture_fee_clocks(rpc: RawRpc, *, chain_id: int, datastore: str, market: str,
                       block: int, block_hash: str) -> dict[str, Any]:
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    header = reader.open()
    timestamp = int(header["timestamp"], 16)
    keys = {"funding_updated_at": storage_key("FUNDING_UPDATED_AT", market),
            "borrowing_updated_at_long": storage_key(
                "CUMULATIVE_BORROWING_FACTOR_UPDATED_AT", market, True)}
    values = {name: reader.uint(datastore, key) for name, key in keys.items()}
    if any(value > timestamp for value in values.values()):
        raise ValueError("pinned fee clock is later than the pinned block timestamp")
    reader.close()
    return {**_base("GmxStep41PinnedFeeClocks", reader, datastore),
            "block_timestamp": timestamp, "keys": keys, "values": values,
            "rpc_transcript": reader.traced.calls}


def capture_borrowing_skip(rpc: RawRpc, *, chain_id: int, datastore: str, block: int,
                           block_hash: str) -> dict[str, Any]:
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    reader.open()
    key = storage_key("SKIP_BORROWING_FEE_FOR_SMALLER_SIDE")
    value = reader.boolean(datastore, key)
    reader.close()
    return {**_base("GmxStep41PinnedBorrowingSkip", reader, datastore), "key": key,
            "skip_borrowing_fee_for_smaller_side": value,
            "rpc_transcript": reader.traced.calls}


def capture_funding_selector(rpc: RawRpc, *, chain_id: int, datastore: str, market: str,
                             block: int, block_hash: str) -> dict[str, Any]:
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    reader.open()
    key = storage_key("FUNDING_INCREASE_FACTOR_PER_SECOND", market)
    value = reader.uint(datastore, key)
    reader.close()
    return {**_base("GmxStep41PinnedFundingSelector", reader, datastore), "key": key,
            "funding_increase_factor_per_second": value,
            "rpc_transcript": reader.traced.calls}


def capture_delegated_contract(rpc: RawRpc, *, schema: str, address_field: str,
                               signature: str, chain_id: int, order_handler: str,
                               block: int, block_hash: str) -> dict[str, Any]:
    """Read an OrderHandler pointer and the pointed-to runtime code at one pin."""
    reader = _PinnedReader(rpc, chain_id=chain_id, block=block, block_hash=block_hash)
    reader.open()
    address = reader.pointer(order_handler, signature)
    runtime = reader.code(address)
    reader.close()
    return {"schema": schema, "version": 1, "chain_id": chain_id,
            "block_number": block, "block_hash": reader.block_hash,
            "order_handler": order_handler, "method": signature,
            address_field: address,
            "runtime_code_keccak": "0x" + keccak256(runtime).hex(),
            "runtime_code_sha256": "0x" + hashlib.sha256(runtime).hexdigest(),
            "rpc_transcript": reader.traced.calls}


def capture_increase_executor(rpc: RawRpc, **kwargs: Any) -> dict[str, Any]:
    return capture_delegated_contract(
        rpc, schema="GmxStep41PinnedExecutor", address_field="executor_address",
        signature="increaseOrderExecutor()", **kwargs)


def capture_swap_handler(rpc: RawRpc, **kwargs: Any) -> dict[str, Any]:
    return capture_delegated_contract(
        rpc, schema="GmxStep41PinnedHandler", address_field="handler_address",
        signature="swapHandler()", **kwargs)


def endpoint_free(record: dict[str, Any]) -> bool:
    """Refuse to write a record that carries a URL or credential-looking text."""
    text = json.dumps(record)
    return "http://" not in text and "https://" not in text
