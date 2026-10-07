from dataclasses import dataclass

import pytest

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.simulation.archive_state import (
    Cell, PinnedArchiveStateReader, _decode, market_cell, named_cell, position_key,
)
from gmx_crypto_bot_v2.simulation.router_preflight import Deployment


ADDRESS = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
HASH = "0x" + "aa" * 32
CODE = "0x6000"
CODE_HASH = "0x" + keccak256(bytes.fromhex(CODE[2:])).hex()


@dataclass
class FakeRpc:
    calls: int = 0
    reorg: bool = False
    value: str = "0x" + "00" * 31 + "01"

    def request(self, method, params):
        if method == "eth_chainId":
            return {"result": "0x1"}
        if method == "eth_getBlockByNumber":
            self.calls += 1
            return {"result": {"number": "0x64", "hash":
                               "0x" + "bb" * 32 if self.reorg and self.calls > 1 else HASH}}
        if method == "eth_getCode":
            return {"result": CODE}
        if method == "eth_call":
            assert params[1] == "0x64"
            assert params[0]["to"] == ADDRESS
            return {"result": self.value}
        raise AssertionError(method)


def reader(rpc):
    deployment = Deployment(1, ROUTER, CODE_HASH, ADDRESS, CODE_HASH,
                            ("EndOfOracleSimulation()",))
    return PinnedArchiveStateReader(rpc, deployment)


def inventory():
    return (named_cell("accrual", "CUMULATIVE_BORROWING_FACTOR", "Uint", ADDRESS, True),)


def test_key_derivation_and_decode():
    from gmx_crypto_bot_v2.domain.swap_keys import key, market_field_key
    cell = named_cell("accrual", "CUMULATIVE_BORROWING_FACTOR", "Uint", ADDRESS, True)
    assert cell.storage_key == key("CUMULATIVE_BORROWING_FACTOR", ADDRESS, True)
    assert market_cell("INDEX_TOKEN", ADDRESS).storage_key == market_field_key(ADDRESS, "INDEX_TOKEN")
    assert _decode("0x" + "ff" * 32, "Int") == -1
    assert _decode("0x" + "00" * 31 + "01", "Bool") is True
    with pytest.raises(ValueError, match="boolean"):
        _decode("0x" + "00" * 31 + "02", "Bool")


def test_pinned_read_and_missing_group():
    rpc = FakeRpc()
    snapshot = reader(rpc).read(100, HASH, inventory())
    assert snapshot.block_hash == HASH
    assert snapshot.values["accrual"]["CUMULATIVE_BORROWING_FACTOR"] == 1
    assert snapshot.complete_for_comparison is False
    with pytest.raises(ValueError, match="empty"):
        reader(FakeRpc()).read(100, HASH, ())
    with pytest.raises(ValueError, match="unknown"):
        named_cell("order", "MADE_UP_KEY", "Uint")


def test_reorg_missing_cell_and_unsupported_kind():
    with pytest.raises(ValueError, match="changed"):
        reader(FakeRpc(reorg=True)).read(100, HASH, inventory())
    with pytest.raises(ValueError, match="32-byte"):
        reader(FakeRpc(value="0x")).read(100, HASH, inventory())
    with pytest.raises(ValueError, match="unsupported"):
        Cell("order", "TYPE", "String", "0x" + "00" * 32)


def test_wrong_chain_or_historical_code_fails_closed():
    class WrongChain(FakeRpc):
        def request(self, method, params):
            if method == "eth_chainId":
                return {"result": "0xa4b1"}
            return super().request(method, params)

    with pytest.raises(ValueError, match="chain"):
        reader(WrongChain()).read(100, HASH, inventory())
    rpc = FakeRpc()
    deployment = Deployment(1, ROUTER, "0x" + "00" * 32, ADDRESS, CODE_HASH,
                            ("EndOfOracleSimulation()",))
    with pytest.raises(ValueError, match="code mismatch"):
        PinnedArchiveStateReader(rpc, deployment).read(100, HASH, inventory())


def test_reader_struct_pin_and_empty_open_position(monkeypatch):
    import gmx_crypto_bot_v2.simulation.archive_state as module
    order = {"account": ADDRESS, "market": ROUTER,
             "initialCollateralToken": ADDRESS, "isLong": True,
             "orderType": 2, "sizeDeltaUsd": 100}
    empty = {"account": "0x" + "00" * 20, "market": "0x" + "00" * 20,
             "collateralToken": "0x" + "00" * 20, "isLong": False,
             "sizeInUsd": 0, "sizeInTokens": 0, "collateralAmount": 0,
             "pendingImpactAmount": 0, "borrowingFactor": 0,
             "fundingFeeAmountPerSize": 0}
    monkeypatch.setattr(module, "_decode_order", lambda _: order)
    monkeypatch.setattr(module, "_decode_position", lambda _: empty)
    rpc = FakeRpc()
    snapshot = reader(rpc).read_order_position(
        block=100, expected_hash=HASH, order_key="0x" + "33" * 32,
        expected_request=order, reader_address=ROUTER, reader_code_hash=CODE_HASH)
    assert snapshot.position_key == position_key(ADDRESS, ROUTER, ADDRESS, True)
    assert snapshot.position["sizeInUsd"] == 0
    with pytest.raises(ValueError, match="preflight request"):
        reader(FakeRpc()).read_order_position(
            block=100, expected_hash=HASH, order_key="0x" + "33" * 32,
            expected_request={**order, "sizeDeltaUsd": 101},
            reader_address=ROUTER, reader_code_hash=CODE_HASH)
    order["orderType"] = 4
    with pytest.raises(ValueError, match="decrease position absent"):
        reader(FakeRpc()).read_order_position(
            block=100, expected_hash=HASH, order_key="0x" + "33" * 32,
            expected_request=order, reader_address=ROUTER, reader_code_hash=CODE_HASH)
