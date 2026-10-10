from dataclasses import dataclass

import pytest

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.crosscheck.archive_state import (
    Cell, PinnedArchiveStateReader, _decode, market_cell, named_cell, position_key,
    fixed_eth_usdc_increase_cells, virtual_token_id_cell,
    virtual_inventory_tokens_cell, execute_order_feature_cell,
    base_increase_size_tokens, aggregate_open_interest_tokens, ArchiveSnapshot,
)
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, _selector


ADDRESS = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
READER = "0x" + "99" * 20
HASH = "0x" + "aa" * 32
CODE = "0x6000"
CODE_HASH = "0x" + keccak256(bytes.fromhex(CODE[2:])).hex()

# DataStore cells are read from the DataStore itself; GMX Reader structs are
# read from the Reader contract, which takes the DataStore as its first
# argument. Routing either call at the wrong contract must stay a test failure.
DATASTORE_SELECTORS = {"0x" + _selector(f"get{kind}(bytes32)").hex()
                       for kind in ("Uint", "Int", "Bool", "Address", "Bytes32")}
READER_SELECTORS = {"0x" + _selector(f"get{struct}(address,bytes32)").hex()
                    for struct in ("Order", "Position")}


@dataclass
class FakeRpc:
    calls: int = 0
    reorg: bool = False
    value: str = "0x" + "00" * 31 + "01"

    def check_eth_call(self, params):
        """Assert the exact target and calldata shape expected per call type."""
        assert params[1] == "0x64", "archive call is not pinned to the block"
        selector = params[0]["data"][:10]
        if selector in READER_SELECTORS:
            assert params[0]["to"] == READER
            assert params[0]["data"][10:74] == "00" * 12 + ADDRESS[2:]
            assert len(params[0]["data"]) == 2 + 8 + 128
        else:
            assert selector in DATASTORE_SELECTORS, f"unknown selector {selector}"
            assert params[0]["to"] == ADDRESS
            assert len(params[0]["data"]) == 2 + 8 + 64

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
            self.check_eth_call(params)
            return {"result": self.value}
        raise AssertionError(method)


def reader(rpc):
    deployment = Deployment(1, ROUTER, CODE_HASH, ADDRESS, CODE_HASH,
                            ("EndOfOracleSimulation()",))
    return PinnedArchiveStateReader(rpc, deployment)


def inventory():
    return (named_cell("accrual", "CUMULATIVE_BORROWING_FACTOR", "Uint", ADDRESS, True),)


def test_fixed_increase_inventory_rejects_unsupported_and_is_not_complete():
    eth = "0x" + "33" * 20
    usdc = "0x" + "44" * 20
    order = {"market": ADDRESS, "initialCollateralToken": usdc,
             "uiFeeReceiver": "0x" + "00" * 20, "orderType": 2, "isLong": True}
    cells = fixed_eth_usdc_increase_cells(order, index_token=eth,
                                          long_token=eth, short_token=usdc)
    assert len({(cell.group, cell.name) for cell in cells}) == len(cells)
    assert len({cell.storage_key for cell in cells}) == len(cells)
    assert {"position_impact_factor_positive", "position_impact_factor_negative",
            "impact_pool"} <= {cell.name for cell in cells}
    assert sum(cell.group == "open_interest" for cell in cells) == 4
    assert not any(cell.group == "referral" for cell in cells)
    with pytest.raises(ValueError, match="supported"):
        fixed_eth_usdc_increase_cells({**order, "orderType": 4},
                                      index_token=eth, long_token=eth, short_token=usdc)
    with pytest.raises(ValueError, match="USDC"):
        fixed_eth_usdc_increase_cells({**order, "initialCollateralToken": eth},
                                      index_token=eth, long_token=eth, short_token=usdc)


def test_source_defined_dynamic_keys_and_base_size_rounding():
    from gmx_crypto_bot_v2.domain.swap_keys import key
    token_id = "0x" + "ab" * 32
    assert virtual_token_id_cell(ADDRESS).storage_key == key("VIRTUAL_TOKEN_ID", ADDRESS)
    assert virtual_inventory_tokens_cell(token_id).storage_key == key(
        "VIRTUAL_INVENTORY_FOR_POSITIONS_IN_TOKENS", token_id)
    assert execute_order_feature_cell(ROUTER, 2).storage_key == key(
        "EXECUTE_ORDER_FEATURE_DISABLED", ROUTER, 2)
    assert base_increase_size_tokens(101, 9, 10, True) == 10
    assert base_increase_size_tokens(101, 9, 10, False) == 12
    assert base_increase_size_tokens(0, 9, 10, True) == 0
    assert base_increase_size_tokens(0, 9, 10, False) == 0
    with pytest.raises(ValueError, match="invalid increase"):
        base_increase_size_tokens(-1, 9, 10, True)
    with pytest.raises(ValueError, match="zero virtual"):
        virtual_inventory_tokens_cell("0x" + "00" * 32)
    with pytest.raises(ValueError, match="unsupported"):
        execute_order_feature_cell(ROUTER, 3)


def test_open_interest_aggregation_requires_all_collateral_buckets():
    long_token, short_token = "0x" + "33" * 20, "0x" + "44" * 20
    values = {"open_interest": {
        f"long:{long_token}": 5, f"long:{short_token}": 7,
        f"short:{long_token}": 11, f"short:{short_token}": 13}}
    snapshot = ArchiveSnapshot(1, 100, HASH, ADDRESS, CODE_HASH, (), values)
    assert aggregate_open_interest_tokens(snapshot, long_token=long_token,
                                          short_token=short_token) == {"long": 12, "short": 24}
    del values["open_interest"][f"short:{short_token}"]
    with pytest.raises(ValueError, match="missing open interest"):
        aggregate_open_interest_tokens(snapshot, long_token=long_token,
                                       short_token=short_token)


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
    import gmx_crypto_bot_v2.crosscheck.archive_state as module
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
    kwargs = dict(block=100, expected_hash=HASH, order_key="0x" + "33" * 32,
                  reader_address=READER, reader_code_hash=CODE_HASH)
    snapshot = reader(rpc).read_order_position(expected_request=order, **kwargs)
    # The Reader structs were read from the Reader, pinned to the same block
    # hash the DataStore reads are bound to (FakeRpc.check_eth_call asserts the
    # target contract and the pinned block for every eth_call).
    assert snapshot.reader == READER and snapshot.reader_code_hash == CODE_HASH.lower()
    assert (snapshot.block_number, snapshot.block_hash) == (100, HASH)
    assert snapshot.order_key == "0x" + "33" * 32
    assert snapshot.reader_timestamp_layout == "order_numbers_12_without_ui_fee_factor"
    assert snapshot.position_key == position_key(ADDRESS, ROUTER, ADDRESS, True)
    assert snapshot.position["sizeInUsd"] == 0
    with pytest.raises(ValueError, match="preflight request"):
        reader(FakeRpc()).read_order_position(
            expected_request={**order, "sizeDeltaUsd": 101}, **kwargs)
    # A block hash that moves between the two pins invalidates the structs.
    with pytest.raises(ValueError, match="changed"):
        reader(FakeRpc(reorg=True)).read_order_position(expected_request=order, **kwargs)
    # An empty position must be empty in every economic field, not just size.
    monkeypatch.setattr(module, "_decode_position",
                        lambda _: {**empty, "collateralAmount": 1})
    with pytest.raises(ValueError, match="nonzero economic fields"):
        reader(FakeRpc()).read_order_position(expected_request=order, **kwargs)
    monkeypatch.setattr(module, "_decode_position", lambda _: empty)
    order["orderType"] = 4
    with pytest.raises(ValueError, match="decrease position absent"):
        reader(FakeRpc()).read_order_position(expected_request=order, **kwargs)


def test_combined_fixed_increase_reads_virtual_and_rejects_late_reorg(monkeypatch):
    import gmx_crypto_bot_v2.crosscheck.archive_state as module
    from gmx_crypto_bot_v2.domain.swap_keys import market_field_key
    eth, usdc, market = ("0x" + "33" * 20, "0x" + "44" * 20,
                         "0x" + "55" * 20)
    virtual_id = "0x" + "66" * 32
    order = {"account": ADDRESS, "market": market, "initialCollateralToken": usdc,
             "uiFeeReceiver": "0x" + "00" * 20, "isLong": True,
             "orderType": 2, "sizeDeltaUsd": 100}
    empty = {"account": "0x" + "00" * 20, "market": "0x" + "00" * 20,
             "collateralToken": "0x" + "00" * 20, "isLong": False,
             "sizeInUsd": 0, "sizeInTokens": 0, "collateralAmount": 0,
             "pendingImpactAmount": 0, "borrowingFactor": 0,
             "fundingFeeAmountPerSize": 0}
    monkeypatch.setattr(module, "_decode_order", lambda _: order)
    monkeypatch.setattr(module, "_decode_position", lambda _: empty)
    addresses = {market_field_key(market, name).lower(): value for name, value in
                 (("MARKET_TOKEN", market), ("INDEX_TOKEN", eth),
                  ("LONG_TOKEN", eth), ("SHORT_TOKEN", usdc))}
    id_key = virtual_token_id_cell(eth).storage_key.lower()
    inventory_key = virtual_inventory_tokens_cell(virtual_id).storage_key.lower()

    class CombinedRpc(FakeRpc):
        missing_inventory = False
        late_reorg = False

        def request(self, method, params):
            if method == "eth_getBlockByNumber" and self.late_reorg and self.calls >= 7:
                return {"result": {"number": "0x64", "hash": "0x" + "bb" * 32}}
            if method == "eth_call":
                self.check_eth_call(params)
            if method == "eth_call" and params[0]["to"] == READER:
                return {"result": "0x" + "00" * 32}
            if method == "eth_call" and params[0]["to"] == ADDRESS:
                storage_key = "0x" + params[0]["data"][-64:]
                key_lower = storage_key.lower()
                if key_lower in addresses:
                    return {"result": "0x" + "00" * 12 + addresses[key_lower][2:]}
                if key_lower == id_key:
                    return {"result": virtual_id}
                if key_lower == inventory_key:
                    return {"result": "0x" if self.missing_inventory else "0x" + "ff" * 32}
                return {"result": "0x" + "00" * 31 + "01"}
            return super().request(method, params)

    kwargs = dict(block=100, expected_hash=HASH, order_key="0x" + "77" * 32,
                  expected_request=order, reader_address=READER,
                  reader_code_hash=CODE_HASH, index_token=eth,
                  long_token=eth, short_token=usdc)
    result = reader(CombinedRpc()).read_fixed_increase_subset(**kwargs)
    assert result.virtual_token_id == virtual_id
    assert result.virtual_inventory_tokens == -1
    assert result.open_interest_tokens == {"long": 2, "short": 2}
    assert result.complete_for_comparison is False
    missing = CombinedRpc()
    missing.missing_inventory = True
    with pytest.raises(ValueError, match="32-byte"):
        reader(missing).read_fixed_increase_subset(**kwargs)
    reorg = CombinedRpc()
    reorg.late_reorg = True
    with pytest.raises(ValueError, match="changed|mismatch"):
        reader(reorg).read_fixed_increase_subset(**kwargs)
