"""Read-only, block-pinned GMX latest-order preflight.

Deployment addresses, historical code hashes, error ABI, and integer oracle
prices are caller-supplied evidence. No method in this module sends a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gmx_crypto_bot_v2.domain.keys import keccak256


class RawRpc(Protocol):
    def request(self, method: str, params: list[Any]) -> dict[str, Any]: ...


def _word(value: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**256:
        raise ValueError("invalid uint256")
    return value.to_bytes(32, "big")


def _hex_bytes(value: str, length: int) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 2 + 2 * length:
        raise ValueError(f"expected {length}-byte hex value")
    return bytes.fromhex(value[2:])


def _address(value: str) -> bytes:
    return _hex_bytes(value, 20)


def _selector(signature: str) -> bytes:
    return keccak256(signature.encode())[:4]


def _key(label: str) -> bytes:
    # Solidity's keccak256(abi.encode(string)); offset, length, padded bytes.
    raw = label.encode()
    return keccak256(_word(32) + _word(len(raw)) + raw.ljust((len(raw) + 31) // 32 * 32, b"\0"))


def _field_key(order_key: bytes, label: str) -> bytes:
    return keccak256(order_key + _key(label))


def latest_key(datastore: str, nonce: int) -> str:
    return "0x" + keccak256(_address(datastore).rjust(32, b"\0") + _word(nonce)).hex()


@dataclass(frozen=True)
class Deployment:
    chain_id: int
    router: str
    router_code_hash: str
    datastore: str
    datastore_code_hash: str
    # Explicitly bound to the historical deployment; names and signatures are
    # not inferred from current source code at runtime.
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        for address in (self.router, self.datastore):
            if not any(_address(address)):
                raise ValueError("zero deployment address")
        for code_hash in (self.router_code_hash, self.datastore_code_hash):
            _hex_bytes(code_hash, 32)
        if "EndOfOracleSimulation()" not in self.errors:
            raise ValueError("deployment error ABI must identify EndOfOracleSimulation()")


@dataclass(frozen=True)
class OracleInput:
    tokens: tuple[str, ...]
    prices: tuple[tuple[int, int], ...]
    min_timestamp: int
    max_timestamp: int
    provenance: str
    scale_proof: str

    def __post_init__(self) -> None:
        if not self.tokens or len(self.tokens) != len(self.prices):
            raise ValueError("oracle tokens and prices must be parallel and nonempty")
        if len({x.lower() for x in self.tokens}) != len(self.tokens):
            raise ValueError("duplicate oracle token")
        for token in self.tokens:
            _address(token)
        for low, high in self.prices:
            _word(low); _word(high)
            if low == 0 or low > high:
                raise ValueError("invalid oracle price range")
        _word(self.min_timestamp); _word(self.max_timestamp)
        if self.min_timestamp > self.max_timestamp or not self.provenance or not self.scale_proof:
            raise ValueError("oracle timestamp/provenance/scale proof missing")


def encode_simulation(prices: OracleInput) -> str:
    """ABI encode simulateExecuteLatestOrder((address[],(uint256,uint256)[],uint256,uint256))."""
    n = len(prices.tokens)
    token_tail = _word(n) + b"".join(_address(t).rjust(32, b"\0") for t in prices.tokens)
    price_tail = _word(n) + b"".join(_word(low) + _word(high) for low, high in prices.prices)
    # One dynamic tuple argument, then its four-word head and two dynamic arrays.
    tuple_head = _word(128) + _word(128 + len(token_tail)) + _word(prices.min_timestamp) + _word(prices.max_timestamp)
    return "0x" + (_selector("simulateExecuteLatestOrder((address[],(uint256,uint256)[],uint256,uint256))")
                   + _word(32) + tuple_head + token_tail + price_tail).hex()


_FIELDS: tuple[tuple[str, str], ...] = (
    ("account", "ACCOUNT"), ("receiver", "RECEIVER"),
    ("cancellationReceiver", "CANCELLATION_RECEIVER"),
    ("callbackContract", "CALLBACK_CONTRACT"), ("uiFeeReceiver", "UI_FEE_RECEIVER"),
    ("market", "MARKET"), ("initialCollateralToken", "INITIAL_COLLATERAL_TOKEN"),
    ("swapPath", "SWAP_PATH"), ("orderType", "ORDER_TYPE"),
    ("decreasePositionSwapType", "DECREASE_POSITION_SWAP_TYPE"),
    ("sizeDeltaUsd", "SIZE_DELTA_USD"),
    ("initialCollateralDeltaAmount", "INITIAL_COLLATERAL_DELTA_AMOUNT"),
    ("triggerPrice", "TRIGGER_PRICE"), ("acceptablePrice", "ACCEPTABLE_PRICE"),
    ("executionFee", "EXECUTION_FEE"), ("callbackGasLimit", "CALLBACK_GAS_LIMIT"),
    ("minOutputAmount", "MIN_OUTPUT_AMOUNT"), ("uiFeeFactor", "UI_FEE_FACTOR"),
    ("validFromTime", "VALID_FROM_TIME"),
    ("updatedAtTime", "UPDATED_AT_TIME"), ("srcChainId", "SRC_CHAIN_ID"),
    ("isLong", "IS_LONG"), ("shouldUnwrapNativeToken", "SHOULD_UNWRAP_NATIVE_TOKEN"),
    ("isFrozen", "IS_FROZEN"), ("autoCancel", "AUTO_CANCEL"),
    ("dataList", "DATA_LIST"),
)
_ADDRESS_FIELDS = {"account", "receiver", "cancellationReceiver", "callbackContract", "uiFeeReceiver", "market", "initialCollateralToken"}
_BOOL_FIELDS = {"isLong", "shouldUnwrapNativeToken", "isFrozen", "autoCancel"}
_ARRAY_FIELDS = {"swapPath": ("getAddressArray(bytes32)", 20), "dataList": ("getBytes32Array(bytes32)", 32)}


def _extract_revert(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("0x"):
        return value
    if isinstance(value, dict):
        for field in ("data", "result", "return", "originalError"):
            found = _extract_revert(value.get(field))
            if found:
                return found
        for nested in value.values():
            found = _extract_revert(nested)
            if found:
                return found
    return None


def decode_revert(raw: str | None, errors: tuple[str, ...]) -> dict[str, Any]:
    if raw is None or raw == "0x":
        return {"outcome": "provider_failure", "reason": "missing_revert_data", "raw_revert": raw}
    try:
        data = bytes.fromhex(raw[2:])
    except (ValueError, TypeError):
        return {"outcome": "provider_failure", "reason": "malformed_revert_data", "raw_revert": raw}
    if len(data) < 4:
        return {"outcome": "provider_failure", "reason": "short_revert_data", "raw_revert": raw}
    names = {_selector(signature): signature for signature in errors}
    signature = names.get(data[:4])
    outcome = ("passed_preflight" if signature == "EndOfOracleSimulation()" else
               "validation_error" if signature else "unknown_contract_error")
    args: list[Any] | None = None
    if signature:
        types = signature[signature.index("(") + 1:-1].split(",")
        if types == [""]:
            types = []
        if all(t in {"address", "bytes32", "bool"} or t.startswith("uint") or t.startswith("int")
               for t in types) and len(data) == 4 + 32 * len(types):
            args = []
            for index, arg_type in enumerate(types):
                word = data[4 + index*32:36 + index*32]
                if arg_type == "address":
                    args.append("0x" + word[-20:].hex())
                elif arg_type == "bytes32":
                    args.append("0x" + word.hex())
                elif arg_type == "bool":
                    args.append(bool(int.from_bytes(word, "big")))
                else:
                    args.append(int.from_bytes(word, "big", signed=arg_type.startswith("int")))
    return {"outcome": outcome, "selector": "0x" + data[:4].hex(),
            "error_signature": signature, "error_arguments": args, "raw_revert": raw}


class RpcFailure(Exception):
    def __init__(self, method: str, error: Any):
        super().__init__(f"{method} failed")
        self.method = method
        self.error = error


class RouterPreflight:
    def __init__(self, rpc: RawRpc, deployment: Deployment):
        self.rpc = rpc
        self.deployment = deployment

    def _rpc(self, method: str, params: list[Any]) -> Any:
        try:
            envelope = self.rpc.request(method, params)
        except (OSError, TimeoutError, ConnectionError) as failure:
            raise RpcFailure(method, {"message": type(failure).__name__}) from failure
        if not isinstance(envelope, dict):
            raise RpcFailure(method, {"message": "malformed RPC envelope"})
        if "error" in envelope:
            raise RpcFailure(method, envelope["error"])
        if "result" not in envelope:
            raise RpcFailure(method, {"message": "missing RPC result"})
        return envelope["result"]

    def _call(self, address: str, data: str, block: int) -> str:
        result = self._rpc("eth_call", [{"to": address, "data": data}, hex(block)])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise RpcFailure("eth_call", {"message": "malformed call result"})
        return result

    def _block_hash(self, block: int) -> str:
        result = self._rpc("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(result, dict) or not isinstance(result.get("hash"), str):
            raise RpcFailure("eth_getBlockByNumber", {"message": "missing block hash"})
        _hex_bytes(result["hash"], 32)
        return result["hash"].lower()

    def _read(self, signature: str, key: bytes, block: int) -> str:
        return self._call(self.deployment.datastore,
                          "0x" + (_selector(signature) + key).hex(), block)

    def _scalar(self, signature: str, key: bytes, block: int) -> int:
        raw = _hex_bytes(self._read(signature, key, block), 32)
        return int.from_bytes(raw, "big")

    def _array(self, signature: str, key: bytes, block: int, size: int) -> list[str]:
        raw = self._read(signature, key, block)
        data = bytes.fromhex(raw[2:])
        if len(data) < 64 or int.from_bytes(data[:32], "big") != 32:
            raise ValueError("malformed datastore array")
        n = int.from_bytes(data[32:64], "big")
        if n > 1024 or len(data) != 64 + n * 32:
            raise ValueError("malformed datastore array length")
        return ["0x" + data[64 + i*32 + (32-size):96 + i*32].hex() for i in range(n)]

    def run(self, candidate: dict[str, Any], oracle: OracleInput) -> dict[str, Any]:
        block = candidate.get("proposed_pin_block")
        key = candidate.get("order_key")
        report: dict[str, Any] = {"order_key": key, "pin_block": block,
                                  "outcome": "evidence_failure"}
        try:
            if candidate.get("selection_skip_reasons"):
                report["reason"] = "selection_ineligible"
                report["selection_skip_reasons"] = candidate["selection_skip_reasons"]
                return report
            if not isinstance(block, int) or block < 0:
                raise ValueError("missing pin block")
            key_bytes = _hex_bytes(key, 32)
            expected = candidate.get("request_at_proposed_pin")
            if not isinstance(expected, dict):
                raise ValueError("missing recorded request")
            # Some Order.Props fields are not emitted in OrderCreated. The pinned
            # DataStore is the authoritative request for the router call. Keep
            # unrecorded fields visible so a later comparison cannot mistake
            # archive-only proof for event-to-chain agreement.
            missing = [name for name, _ in _FIELDS if name not in expected]
            report["archive_only_request_fields"] = missing
            if not expected or any(name not in expected for name in (
                "account", "market", "orderType", "isLong", "initialCollateralToken",
                "sizeDeltaUsd", "acceptablePrice", "triggerPrice"
            )):
                raise ValueError("missing core recorded request fields")
            if int(self._rpc("eth_chainId", []), 16) != self.deployment.chain_id:
                raise ValueError("unexpected chain ID")
            before = self._block_hash(block)
            report["pin_block_hash"] = before
            if candidate.get("proposed_pin_hash") and candidate["proposed_pin_hash"].lower() != before:
                raise ValueError("recorded pin hash mismatch")
            for label, address, expected_hash in (
                ("router", self.deployment.router, self.deployment.router_code_hash),
                ("datastore", self.deployment.datastore, self.deployment.datastore_code_hash),
            ):
                code = self._rpc("eth_getCode", [address, hex(block)])
                if not isinstance(code, str) or code == "0x" or "0x" + keccak256(bytes.fromhex(code[2:])).hex() != expected_hash.lower():
                    raise ValueError(f"{label} historical code hash mismatch")
            nonce = self._scalar("getUint(bytes32)", _key("NONCE"), block)
            derived = latest_key(self.deployment.datastore, nonce)
            report.update({"nonce": nonce, "derived_latest_key": derived,
                           "latest": derived.lower() == key.lower()})
            if not report["latest"]:
                report.update(outcome="ineligible_latest_request", reason="global_nonce_key_mismatch")
                return report
            present = self._scalar("containsBytes32(bytes32,bytes32)",
                                   _key("ORDER_LIST") + key_bytes, block)
            report["pending"] = present == 1
            if not report["pending"]:
                report.update(outcome="ineligible_latest_request", reason="order_not_pending")
                return report
            on_chain: dict[str, Any] = {}
            for name, label in _FIELDS:
                field_key = _field_key(key_bytes, label)
                if name in _ARRAY_FIELDS:
                    signature, size = _ARRAY_FIELDS[name]
                    on_chain[name] = self._array(signature, field_key, block, size)
                elif name in _ADDRESS_FIELDS:
                    value = self._scalar("getAddress(bytes32)", field_key, block)
                    on_chain[name] = "0x" + value.to_bytes(32, "big")[-20:].hex()
                elif name in _BOOL_FIELDS:
                    value = self._scalar("getBool(bytes32)", field_key, block)
                    if value not in (0, 1):
                        raise ValueError("malformed datastore bool")
                    on_chain[name] = bool(value)
                else:
                    on_chain[name] = self._scalar("getUint(bytes32)", field_key, block)
            report["on_chain_request"] = on_chain
            mismatched = [name for name, _ in _FIELDS if name in expected
                          if _normalize(on_chain[name]) != _normalize(expected[name])]
            if mismatched:
                report.update(outcome="evidence_failure", reason="request_field_mismatch",
                              mismatched_fields=mismatched)
                return report
            calldata = encode_simulation(oracle)
            report["oracle"] = {"tokens": oracle.tokens, "prices": oracle.prices,
                                "min_timestamp": oracle.min_timestamp,
                                "max_timestamp": oracle.max_timestamp,
                                "provenance": oracle.provenance, "scale_proof": oracle.scale_proof}
            report["router"] = self.deployment.router
            report["calldata"] = calldata
            try:
                result = self._call(self.deployment.router, calldata, block)
            except RpcFailure as failure:
                report.update(decode_revert(_extract_revert(failure.error), self.deployment.errors))
                report["rpc_error"] = failure.error
            else:
                report.update(outcome="unknown_contract_error", reason="simulation_returned_without_revert",
                              raw_result=result)
            return report
        except (RpcFailure, ValueError, TypeError) as failure:
            report.update(outcome="provider_failure" if isinstance(failure, RpcFailure) else "evidence_failure",
                          reason=str(failure))
            if isinstance(failure, RpcFailure):
                report["rpc_error"] = failure.error
            return report
        finally:
            if isinstance(block, int) and block >= 0 and "pin_block_hash" in report:
                try:
                    after = self._block_hash(block)
                    report["pin_block_hash_after"] = after
                    if after != report["pin_block_hash"]:
                        report.update(outcome="evidence_failure", reason="block_hash_changed")
                except (RpcFailure, ValueError, TypeError) as failure:
                    report.update(outcome="provider_failure", reason="post_call_block_hash_unavailable")


def _normalize(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("0x"):
        return value.lower()
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value
