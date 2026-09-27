"""Capture block-pinned opening orders, positions and market state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class Rpc(Protocol):
    def call(self, method: str, params: list[Any]) -> Any: ...

    def call_many(self, method: str, params_list: list[list[Any]]) -> list[Any]: ...


ZERO_ADDRESS = "0x" + "0" * 40


def _word(value: int) -> str:
    return f"{value:064x}"


def _address_word(value: str) -> str:
    normalized = value.removeprefix("0x")
    if len(normalized) != 40:
        raise ValueError(f"invalid address: {value}")
    return normalized.lower().rjust(64, "0")


def _bytes32_word(value: str) -> str:
    normalized = value.removeprefix("0x")
    if len(normalized) != 64:
        raise ValueError(f"invalid bytes32 value: {value}")
    return normalized.lower()


def _words(payload: str) -> list[str]:
    encoded = payload.removeprefix("0x")
    if len(encoded) % 64:
        raise ValueError("contract result is not ABI word-aligned")
    return [encoded[index : index + 64] for index in range(0, len(encoded), 64)]


def _uint(word: str) -> int:
    return int(word, 16)


def _int(word: str) -> int:
    value = _uint(word)
    return value - (1 << 256) if value >= 1 << 255 else value


def _address(word: str) -> str:
    return "0x" + word[-40:].lower()


@dataclass
class HistoricalContractReader:
    rpc: Rpc
    block_number: int

    def __post_init__(self) -> None:
        self._hash_cache: dict[str, str] = {}

    def hash_bytes(self, value: bytes) -> str:
        encoded = "0x" + value.hex()
        if encoded not in self._hash_cache:
            self._hash_cache[encoded] = str(
                self.rpc.call("web3_sha3", [encoded])
            ).lower()
        return self._hash_cache[encoded]

    def selector(self, signature: str) -> str:
        return self.hash_bytes(signature.encode("ascii"))[2:10]

    def key(self, name: str, *arguments: str) -> str:
        raw_name = name.encode("utf-8")
        padding = b"\0" * ((32 - len(raw_name) % 32) % 32)
        encoded_string = (
            bytes.fromhex(_word(32) + _word(len(raw_name))) + raw_name + padding
        )
        base = self.hash_bytes(encoded_string)
        if not arguments:
            return base
        encoded = bytes.fromhex(_bytes32_word(base) + "".join(arguments))
        return self.hash_bytes(encoded)

    def call(self, address: str, signature: str, argument_words: list[str]) -> str:
        data = "0x" + self.selector(signature) + "".join(argument_words)
        result = self.rpc.call(
            "eth_call",
            [{"to": address, "data": data}, hex(self.block_number)],
        )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ValueError(f"invalid eth_call result for {signature}")
        return result

    def call_many(
        self, address: str, signature: str, arguments: list[list[str]]
    ) -> list[str]:
        selector = self.selector(signature)
        params_list = [
            [
                {"to": address, "data": "0x" + selector + "".join(argument_words)},
                hex(self.block_number),
            ]
            for argument_words in arguments
        ]
        results = self.rpc.call_many("eth_call", params_list)
        if any(
            not isinstance(result, str) or not result.startswith("0x")
            for result in results
        ):
            raise ValueError(f"invalid batched eth_call result for {signature}")
        return results

    def data_store_uint(self, data_store: str, key: str) -> int:
        return _uint(
            _words(self.call(data_store, "getUint(bytes32)", [_bytes32_word(key)]))[0]
        )

    def data_store_int(self, data_store: str, key: str) -> int:
        return _int(
            _words(self.call(data_store, "getInt(bytes32)", [_bytes32_word(key)]))[0]
        )

    def bytes32_list(self, data_store: str, key: str, *, page_size: int) -> list[str]:
        count = _uint(
            _words(
                self.call(data_store, "getBytes32Count(bytes32)", [_bytes32_word(key)])
            )[0]
        )
        values: list[str] = []
        for start in range(0, count, page_size):
            end = min(start + page_size, count)
            result = _words(
                self.call(
                    data_store,
                    "getBytes32ValuesAt(bytes32,uint256,uint256)",
                    [_bytes32_word(key), _word(start), _word(end)],
                )
            )
            if len(result) < 2 or _uint(result[0]) != 32:
                raise ValueError("invalid bytes32[] result from DataStore")
            length = _uint(result[1])
            page = result[2 : 2 + length]
            if len(page) != length:
                raise ValueError("truncated bytes32[] result from DataStore")
            values.extend("0x" + value.lower() for value in page)
        return values


class OpeningCheckpointCollector:
    """Capture the state needed before the first event in a bounded replay."""

    def __init__(
        self,
        rpc: Rpc,
        spec: dict[str, Any],
        *,
        page_size: int = 500,
        rpc_batch_size: int = 100,
    ) -> None:
        self.rpc = rpc
        self.spec = spec
        self.page_size = page_size
        self.rpc_batch_size = rpc_batch_size
        self.market = str(spec["deployment"]["market_token_address"]).lower()
        self.data_store = str(spec["contracts"]["data_store"]).lower()
        self.reader = str(spec["contracts"]["reader"]).lower()
        self.collateral_tokens = {
            str(spec["tokens"]["long"]["address"]).lower(),
            str(spec["tokens"]["short"]["address"]).lower(),
        }

    def capture(self, block_number: int) -> dict[str, Any]:
        reader = HistoricalContractReader(self.rpc, block_number)
        header = self.rpc.call("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(header, dict) or not header.get("hash"):
            raise ValueError(
                f"archive RPC did not return checkpoint block {block_number}"
            )

        positions = self._positions(reader)
        orders = self._orders(reader)
        open_interest_usd, open_interest_tokens = self._open_interest(reader)
        borrowing = self._borrowing(reader)
        funding = self._funding(reader)
        return {
            "schema": "GmxOpeningStateCheckpoint",
            "version": 1,
            "complete": True,
            "block_number": block_number,
            "block_hash": str(header["hash"]).lower(),
            "block_timestamp": header.get("timestamp"),
            "market_token_address": self.market,
            "oracle": {},
            "configuration": {},
            "open_interest_usd": open_interest_usd,
            "open_interest_tokens": open_interest_tokens,
            "borrowing": borrowing,
            "funding": funding,
            "orders": orders,
            "positions": positions,
            "fees": {},
            "coverage": {
                "active_order_set": "complete global ORDER_LIST filtered by decoded market",
                "active_position_set": "complete global POSITION_LIST filtered by decoded market",
                "market_aggregates": "open interest, cumulative borrowing, and funding amount-per-size",
                "oracle": "transaction-scoped GMX prices begin with OraclePriceUpdate events in the recording",
                "configuration": "versioned by Set* events; pinned spec snapshot is not applied retroactively",
            },
        }

    def _positions(self, reader: HistoricalContractReader) -> dict[str, dict[str, Any]]:
        keys = reader.bytes32_list(
            self.data_store, reader.key("POSITION_LIST"), page_size=self.page_size
        )
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(keys), self.rpc_batch_size):
            page = keys[start : start + self.rpc_batch_size]
            encoded_positions = reader.call_many(
                self.reader,
                "getPosition(address,bytes32)",
                [[_address_word(self.data_store), _bytes32_word(key)] for key in page],
            )
            for key, encoded in zip(page, encoded_positions, strict=True):
                position = _decode_position(encoded)
                if position["market"] == self.market:
                    result[key] = position
        return result

    def _orders(self, reader: HistoricalContractReader) -> dict[str, dict[str, Any]]:
        keys = reader.bytes32_list(
            self.data_store, reader.key("ORDER_LIST"), page_size=self.page_size
        )
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(keys), self.rpc_batch_size):
            page = keys[start : start + self.rpc_batch_size]
            encoded_orders = reader.call_many(
                self.reader,
                "getOrder(address,bytes32)",
                [[_address_word(self.data_store), _bytes32_word(key)] for key in page],
            )
            for key, encoded in zip(page, encoded_orders, strict=True):
                order = _decode_order(encoded)
                if order["market"] == self.market:
                    result[key] = {
                        "opening_checkpoint": order,
                        "created": None,
                        "created_coordinate": [reader.block_number, -1, -1, -1],
                        "updates": [],
                        "terminal": None,
                    }
        return result

    def _open_interest(
        self, reader: HistoricalContractReader
    ) -> tuple[dict[str, int], dict[str, int]]:
        usd: dict[str, int] = {}
        tokens: dict[str, int] = {}
        for collateral in sorted(self.collateral_tokens):
            for is_long in (False, True):
                side = "long" if is_long else "short"
                label = f"{collateral}:{side}"
                arguments = (
                    _address_word(self.market),
                    _address_word(collateral),
                    _word(int(is_long)),
                )
                usd[label] = reader.data_store_uint(
                    self.data_store, reader.key("OPEN_INTEREST", *arguments)
                )
                tokens[label] = reader.data_store_uint(
                    self.data_store, reader.key("OPEN_INTEREST_IN_TOKENS", *arguments)
                )
        return usd, tokens

    def _borrowing(self, reader: HistoricalContractReader) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for is_long in (False, True):
            side = "long" if is_long else "short"
            key = reader.key(
                "CUMULATIVE_BORROWING_FACTOR",
                _address_word(self.market),
                _word(int(is_long)),
            )
            updated_key = reader.key(
                "CUMULATIVE_BORROWING_FACTOR_UPDATED_AT",
                _address_word(self.market),
                _word(int(is_long)),
            )
            result[side] = {
                "cumulative_factor": {
                    "market": self.market,
                    "isLong": is_long,
                    "nextCumulativeBorrowingFactor": reader.data_store_uint(
                        self.data_store, key
                    ),
                    "updatedAt": reader.data_store_uint(self.data_store, updated_key),
                }
            }
        return result

    def _funding(self, reader: HistoricalContractReader) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for collateral in sorted(self.collateral_tokens):
            for is_long in (False, True):
                side = "long" if is_long else "short"
                label = f"{collateral}:{side}"
                arguments = (
                    _address_word(self.market),
                    _address_word(collateral),
                    _word(int(is_long)),
                )
                fee_key = reader.key("FUNDING_FEE_AMOUNT_PER_SIZE", *arguments)
                claimable_key = reader.key(
                    "CLAIMABLE_FUNDING_AMOUNT_PER_SIZE", *arguments
                )
                result[label] = {
                    "fee_amount_per_size": {
                        "market": self.market,
                        "collateralToken": collateral,
                        "isLong": is_long,
                        "nextValue": reader.data_store_uint(self.data_store, fee_key),
                    },
                    "claimable_amount_per_size": reader.data_store_uint(
                        self.data_store, claimable_key
                    ),
                }
        return result


def _decode_position(payload: str) -> dict[str, Any]:
    words = _words(payload)
    if len(words) < 14:
        raise ValueError("truncated Reader.getPosition result")
    return {
        "account": _address(words[0]),
        "market": _address(words[1]),
        "collateralToken": _address(words[2]),
        "sizeInUsd": _uint(words[3]),
        "sizeInTokens": _uint(words[4]),
        "collateralAmount": _uint(words[5]),
        "pendingImpactAmount": _int(words[6]),
        "borrowingFactor": _uint(words[7]),
        "fundingFeeAmountPerSize": _uint(words[8]),
        "longTokenClaimableFundingAmountPerSize": _uint(words[9]),
        "shortTokenClaimableFundingAmountPerSize": _uint(words[10]),
        "increasedAtTime": _uint(words[11]),
        "decreasedAtTime": _uint(words[12]),
        "isLong": bool(_uint(words[13])),
    }


def _decode_order(payload: str) -> dict[str, Any]:
    words = _words(payload)
    if not words:
        raise ValueError("empty Reader.getOrder result")
    root = _uint(words[0]) // 32
    if root + 18 > len(words):
        raise ValueError("truncated Reader.getOrder result")
    addresses_base = root + _uint(words[root]) // 32
    if addresses_base + 8 > len(words):
        raise ValueError("truncated Reader.getOrder addresses")
    swap_path_base = addresses_base + _uint(words[addresses_base + 7]) // 32
    swap_path_length = _uint(words[swap_path_base])
    swap_path = [
        _address(word)
        for word in words[swap_path_base + 1 : swap_path_base + 1 + swap_path_length]
    ]
    numbers = root + 1
    # Deployed Reader results include one additional numeric slot after the
    # twelve named fields. Detect the tuple length from the dynamic data-list
    # offset instead of assuming the current source-tree layout.
    flags = -1
    data_list_base = -1
    for number_count in (13, 12):
        candidate_flags = numbers + number_count
        data_list_slot = candidate_flags + 4
        if data_list_slot >= len(words):
            continue
        offset = _uint(words[data_list_slot])
        candidate_base = root + offset // 32
        if (
            offset % 32
            or offset < (number_count + 6) * 32
            or candidate_base >= len(words)
        ):
            continue
        if any(
            _uint(word) not in (0, 1)
            for word in words[candidate_flags : candidate_flags + 4]
        ):
            continue
        flags = candidate_flags
        data_list_base = candidate_base
        break
    if flags < 0:
        raise ValueError("invalid Reader.getOrder flags or data-list offset")
    data_list_length = _uint(words[data_list_base])
    if data_list_base + 1 + data_list_length > len(words):
        raise ValueError("truncated Reader.getOrder data list")
    data_list = [
        "0x" + word.lower()
        for word in words[data_list_base + 1 : data_list_base + 1 + data_list_length]
    ]
    return {
        "account": _address(words[addresses_base]),
        "receiver": _address(words[addresses_base + 1]),
        "cancellationReceiver": _address(words[addresses_base + 2]),
        "callbackContract": _address(words[addresses_base + 3]),
        "uiFeeReceiver": _address(words[addresses_base + 4]),
        "market": _address(words[addresses_base + 5]),
        "initialCollateralToken": _address(words[addresses_base + 6]),
        "swapPath": swap_path,
        "orderType": _uint(words[numbers]),
        "decreasePositionSwapType": _uint(words[numbers + 1]),
        "sizeDeltaUsd": _uint(words[numbers + 2]),
        "initialCollateralDeltaAmount": _uint(words[numbers + 3]),
        "triggerPrice": _uint(words[numbers + 4]),
        "acceptablePrice": _uint(words[numbers + 5]),
        "executionFee": _uint(words[numbers + 6]),
        "callbackGasLimit": _uint(words[numbers + 7]),
        "minOutputAmount": _uint(words[numbers + 8]),
        "updatedAtTime": _uint(words[numbers + 9]),
        "validFromTime": _uint(words[numbers + 10]),
        "srcChainId": _uint(words[numbers + 11]),
        "isLong": bool(_uint(words[flags])),
        "shouldUnwrapNativeToken": bool(_uint(words[flags + 1])),
        "isFrozen": bool(_uint(words[flags + 2])),
        "autoCancel": bool(_uint(words[flags + 3])),
        "dataList": data_list,
    }
