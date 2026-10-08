"""Read and bind individual GMX DataStore cells at one historical block.

This low-level reader makes no completeness claim. A fixed, audited inventory
for a supported order type is still needed before comparison can use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.domain.swap_keys import key as storage_key, market_field_key
from gmx_crypto_bot_v2.collection.checkpoint import _decode_order, _decode_position
from gmx_crypto_bot_v2.domain.constants import INCREASE_ORDER_TYPES, DECREASE_ORDER_TYPES
from gmx_crypto_bot_v2.crosscheck.router_preflight import (
    Deployment, RawRpc, _hex_bytes, _selector,
)


REQUIRED_GROUPS = frozenset({
    "order", "position", "market", "configuration", "open_interest",
    "liquidity", "impact", "accrual", "referral", "virtual_inventory",
    "feature_flags",
})


@dataclass(frozen=True)
class Cell:
    group: str
    name: str
    kind: str
    storage_key: str

    def __post_init__(self) -> None:
        if self.group not in REQUIRED_GROUPS or self.kind not in {"Uint", "Int", "Bool", "Address", "Bytes32"}:
            raise ValueError("unsupported cell group or kind")
        _hex_bytes(self.storage_key, 32)


@dataclass(frozen=True)
class ArchiveSnapshot:
    chain_id: int
    block_number: int
    block_hash: str
    datastore: str
    datastore_code_hash: str
    cells: tuple[Cell, ...]
    values: dict[str, dict[str, int | bool | str]]

    @property
    def complete_for_comparison(self) -> bool:
        # A caller-provided inventory cannot prove order-specific completeness.
        return False


@dataclass(frozen=True)
class ArchivedOrderPosition:
    block_number: int
    block_hash: str
    order_key: str
    position_key: str
    reader: str
    reader_code_hash: str
    order: dict[str, Any]
    position: dict[str, Any]


def position_key(account: str, market: str, collateral: str, is_long: bool) -> str:
    """PositionUtils key: keccak256(abi.encode(account,market,collateral,isLong))."""
    if type(is_long) is not bool:
        raise ValueError("invalid position side")
    words = (_hex_bytes(account, 20).rjust(32, b"\0") +
             _hex_bytes(market, 20).rjust(32, b"\0") +
             _hex_bytes(collateral, 20).rjust(32, b"\0") +
             int(is_long).to_bytes(32, "big"))
    return "0x" + keccak256(words).hex()


_KNOWN_KEYS = frozenset({
    "CUMULATIVE_BORROWING_FACTOR", "FUNDING_FEE_AMOUNT_PER_SIZE",
    "OPEN_INTEREST", "OPEN_INTEREST_IN_TOKENS", "POOL_AMOUNT",
    "POSITION_IMPACT_POOL_AMOUNT", "POSITION_IMPACT_FACTOR",
    "POSITION_IMPACT_EXPONENT_FACTOR", "MAX_POSITION_IMPACT_FACTOR",
    "MAX_LENDABLE_IMPACT_FACTOR", "MAX_LENDABLE_IMPACT_USD",
    "POSITION_FEE_FACTOR", "MAX_UI_FEE_FACTOR",
})


def named_cell(group: str, name: str, kind: str, *args: str | bool) -> Cell:
    """Derive a Keys.sol style key locally; never trust a supplied slot."""
    if name not in _KNOWN_KEYS:
        raise ValueError("unknown GMX storage key name")
    return Cell(group, name, kind, storage_key(name, *args))


def market_cell(name: str, market: str) -> Cell:
    """MarketStoreUtils puts the market address before its field base key."""
    if name not in {"MARKET_TOKEN", "INDEX_TOKEN", "LONG_TOKEN", "SHORT_TOKEN"}:
        raise ValueError("unsupported market field")
    return Cell("market", name, "Address", market_field_key(market, name))


def _decode(raw: Any, kind: str) -> int | bool | str:
    word = _hex_bytes(raw, 32)
    value = int.from_bytes(word, "big")
    if kind == "Bool":
        if value > 1:
            raise ValueError("invalid boolean archive cell")
        return bool(value)
    if kind == "Int":
        return value - 2**256 if value >= 2**255 else value
    if kind == "Address":
        if value >= 2**160:
            raise ValueError("invalid address archive cell")
        return "0x" + word[-20:].hex()
    if kind == "Bytes32":
        return "0x" + word.hex()
    return value


class PinnedArchiveStateReader:
    """Read an explicit cell inventory, rejecting omissions and changed blocks."""

    def __init__(self, rpc: RawRpc, deployment: Deployment):
        self.rpc, self.deployment = rpc, deployment

    def _rpc(self, method: str, params: list[Any]) -> Any:
        response = self.rpc.request(method, params)
        if not isinstance(response, dict) or "error" in response or "result" not in response:
            raise OSError(f"{method} failed")
        return response["result"]

    def _block_hash(self, block: int) -> str:
        header = self._rpc("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(header, dict) or int(header.get("number", "-1"), 16) != block:
            raise ValueError("archive block number mismatch")
        return "0x" + _hex_bytes(header.get("hash"), 32).hex()

    def read(self, block: int, expected_hash: str, cells: tuple[Cell, ...]) -> ArchiveSnapshot:
        if type(block) is not int or block < 0:
            raise ValueError("invalid archive block")
        expected_hash = "0x" + _hex_bytes(expected_hash, 32).hex()
        if not cells:
            raise ValueError("empty archive cell inventory")
        identities = [(cell.group, cell.name) for cell in cells]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate archive cell")
        if int(self._rpc("eth_chainId", []), 16) != self.deployment.chain_id:
            raise ValueError("archive chain mismatch")
        before = self._block_hash(block)
        if before != expected_hash:
            raise ValueError("archive block hash mismatch")
        for address, expected in ((self.deployment.datastore, self.deployment.datastore_code_hash),
                                  (self.deployment.router, self.deployment.router_code_hash)):
            code = self._rpc("eth_getCode", [address, hex(block)])
            if not isinstance(code, str) or code == "0x":
                raise ValueError("missing historical deployment code")
            if "0x" + keccak256(bytes.fromhex(code[2:])).hex() != expected.lower():
                raise ValueError("historical deployment code mismatch")
        values: dict[str, dict[str, int | bool | str]] = {group: {} for group in REQUIRED_GROUPS}
        for cell in cells:
            signature = f"get{cell.kind}(bytes32)"
            data = "0x" + (_selector(signature) + _hex_bytes(cell.storage_key, 32)).hex()
            raw = self._rpc("eth_call", [{"to": self.deployment.datastore, "data": data}, hex(block)])
            values[cell.group][cell.name] = _decode(raw, cell.kind)
        if self._block_hash(block) != before:
            raise ValueError("archive block changed during read")
        return ArchiveSnapshot(self.deployment.chain_id, block, before,
                               self.deployment.datastore, self.deployment.datastore_code_hash,
                               cells, values)

    def read_order_position(self, *, block: int, expected_hash: str, order_key: str,
                            expected_request: dict[str, Any], reader_address: str,
                            reader_code_hash: str) -> ArchivedOrderPosition:
        """Read two structs through GMX Reader at the same block boundary.

        Derive the position key from the archived order. A first increase can
        have no position; a decrease must have one.
        """
        _hex_bytes(order_key, 32)
        _hex_bytes(reader_address, 20); _hex_bytes(reader_code_hash, 32)
        if int(self._rpc("eth_chainId", []), 16) != self.deployment.chain_id:
            raise ValueError("archive chain mismatch")
        before = self._block_hash(block)
        if before != "0x" + _hex_bytes(expected_hash, 32).hex():
            raise ValueError("archive block hash mismatch")
        for address, expected in ((self.deployment.datastore, self.deployment.datastore_code_hash),
                                  (self.deployment.router, self.deployment.router_code_hash),
                                  (reader_address, reader_code_hash)):
            code = self._rpc("eth_getCode", [address, hex(block)])
            if not isinstance(code, str) or code == "0x" or \
                    "0x" + keccak256(bytes.fromhex(code[2:])).hex() != expected.lower():
                raise ValueError("historical deployment code mismatch")
        def fetch(signature: str, item_key: str) -> str:
            data = "0x" + (_selector(signature) +
                           _hex_bytes(self.deployment.datastore, 20).rjust(32, b"\0") +
                           _hex_bytes(item_key, 32)).hex()
            raw = self._rpc("eth_call", [{"to": reader_address, "data": data}, hex(block)])
            if not isinstance(raw, str) or not raw.startswith("0x"):
                raise ValueError("missing Reader struct result")
            return raw
        order = _decode_order(fetch("getOrder(address,bytes32)", order_key))
        if not isinstance(expected_request, dict) or set(order) != set(expected_request):
            raise ValueError("incomplete expected Reader order")
        if any((actual.lower() != expected_request[name].lower() if isinstance(actual, str)
                else actual != expected_request[name]) for name, actual in order.items()):
            raise ValueError("Reader order differs from preflight request")
        kind = order["orderType"]
        if kind not in INCREASE_ORDER_TYPES | DECREASE_ORDER_TYPES:
            raise ValueError("unsupported order type")
        if type(order["isLong"]) is not bool:
            raise ValueError("invalid order side")
        derived_position_key = position_key(order["account"], order["market"],
                                            order["initialCollateralToken"], order["isLong"])
        position = _decode_position(fetch("getPosition(address,bytes32)", derived_position_key))
        if position["sizeInUsd"] > 0:
            if any(order.get(name, "").lower() != position.get(other, "").lower()
                   for name, other in (("account", "account"), ("market", "market"),
                                       ("initialCollateralToken", "collateralToken"))):
                raise ValueError("order and position identities differ")
            if position["isLong"] is not order["isLong"]:
                raise ValueError("order and position sides differ")
        elif kind in DECREASE_ORDER_TYPES:
            raise ValueError("decrease position absent at pinned block")
        elif any(position[name] for name in ("sizeInTokens", "collateralAmount",
                                               "pendingImpactAmount", "borrowingFactor",
                                               "fundingFeeAmountPerSize")):
            raise ValueError("empty position has nonzero economic fields")
        if self._block_hash(block) != before:
            raise ValueError("archive block changed during read")
        return ArchivedOrderPosition(block, before, order_key.lower(), derived_position_key,
                                     reader_address.lower(), reader_code_hash.lower(), order, position)
