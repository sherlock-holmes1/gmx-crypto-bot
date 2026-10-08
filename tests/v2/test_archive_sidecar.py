"""The targeted collector must fail closed before any comparison claim."""

from gmx_crypto_bot_v2.crosscheck.archive_sidecar import collect_increase_sidecar
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, _key, _selector
from gmx_crypto_bot_v2.crosscheck.archive_state import PinnedArchiveStateReader
from gmx_crypto_bot_v2.domain.keys import keccak256


CODE = "0x6000"
CODE_HASH = "0x" + keccak256(bytes.fromhex(CODE[2:])).hex()
HASH = "0x" + "aa" * 32
KEY = "0x" + "33" * 32
ROUTER = "0x" + "22" * 20
STORE = "0x" + "11" * 20
READER = "0x" + "44" * 20
TOKEN = "0x" + "55" * 20


def candidate():
    return {"recording": "recordings/example", "order_key": KEY,
            "proposed_pin_block": 100, "proposed_pin_hash": HASH,
            "creation": {"block_number": 100, "log_index": 1},
            "request_at_proposed_pin": {
                "key": KEY, "orderType": 2, "account": ROUTER,
                "market": STORE, "isLong": True,
                "initialCollateralToken": STORE, "sizeDeltaUsd": 100,
                "acceptablePrice": 20, "triggerPrice": 0},
            "selection_skip_reasons": []}


def deployment():
    return Deployment(1, ROUTER, CODE_HASH, STORE, CODE_HASH,
                      ("EndOfOracleSimulation()",))


def collect(rpc):
    return collect_increase_sidecar(
        rpc, deployment(), candidate(), reader_address=READER,
        reader_code_hash=CODE_HASH, index_token=TOKEN,
        long_token=TOKEN, short_token=STORE)


class GateRpc:
    def __init__(self, *, reorg=False, missing_code=False, wrong_number=False,
                 late_wrong_number=False):
        self.calls = []
        self.reorg = reorg
        self.missing_code = missing_code
        self.wrong_number = wrong_number
        self.late_wrong_number = late_wrong_number
        self.headers = 0

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return {"result": "0x1"}
        if method == "eth_getBlockByNumber":
            assert params == ["0x64", False]
            self.headers += 1
            number = "0x65" if self.wrong_number or (
                self.late_wrong_number and self.headers > 1) else "0x64"
            return {"result": {"number": number,
                               "hash": "0x" + "bb" * 32
                               if self.reorg and self.headers > 1 else HASH}}
        if method == "eth_getCode":
            assert params[1] == "0x64"
            return {"result": "0x" if self.missing_code else CODE}
        if method == "eth_call":
            assert params[0]["to"] == STORE and params[1] == "0x64"
            data = params[0]["data"]
            if data == "0x" + (_selector("getUint(bytes32)") + _key("NONCE")).hex():
                return {"result": "0x" + "00" * 31 + "01"}
            assert data.startswith("0x" + _selector("containsBytes32(bytes32,bytes32)").hex())
            return {"result": "0x" + "00" * 31 + "01"}
        raise AssertionError(method)


def test_ineligible_gate_records_precise_calls_and_never_claims_readiness():
    rpc = GateRpc()
    result = collect(rpc)
    assert result["status"] == "ineligible_latest_request"
    assert result["router_gate"]["pending"] is True
    assert result["router_gate"]["latest"] is False
    assert result["ready_for_comparison"] is False
    assert result["pin"]["hash_after"] == HASH
    assert all(method in {"eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call"}
               for method, _ in rpc.calls)
    assert any(call["method"] == "eth_call" for call in result["rpc_transcript"])


def test_reorg_and_missing_code_fail_closed():
    changed = collect(GateRpc(reorg=True))
    assert changed["status"] == "unavailable"
    assert changed["failure"]["type"] == "Reorg"
    missing = collect(GateRpc(missing_code=True))
    assert missing["status"] == "unavailable"
    assert "code" in missing["failure"]["reason"]


def test_recorded_hash_mismatch_stops_before_code_reads():
    rpc = GateRpc()
    row = candidate()
    row["proposed_pin_hash"] = "0x" + "ff" * 32
    result = collect_increase_sidecar(
        rpc, deployment(), row, reader_address=READER,
        reader_code_hash=CODE_HASH, index_token=TOKEN,
        long_token=TOKEN, short_token=STORE)
    assert result["status"] == "unavailable"
    assert "hash mismatch" in result["failure"]["reason"]
    assert not any(method == "eth_getCode" for method, _ in rpc.calls)


def test_recorded_selection_and_core_fields_fail_before_archive_reads():
    rpc = GateRpc()
    row = candidate()
    row["selection_skip_reasons"] = ["same_block_terminal", "decode_gap"]
    result = collect_increase_sidecar(
        rpc, deployment(), row, reader_address=READER,
        reader_code_hash=CODE_HASH, index_token=TOKEN,
        long_token=TOKEN, short_token=STORE)
    assert result["selection_skip_reasons"] == ["same_block_terminal", "decode_gap"]
    assert result["status"] == "unavailable"
    assert rpc.calls == []
    row = candidate()
    del row["request_at_proposed_pin"]["acceptablePrice"]
    missing = collect_increase_sidecar(
        rpc, deployment(), row, reader_address=READER,
        reader_code_hash=CODE_HASH, index_token=TOKEN,
        long_token=TOKEN, short_token=STORE)
    assert "acceptablePrice" in missing["failure"]["reason"]
    assert rpc.calls == []


def test_unmapped_recorded_field_rejected_and_archive_only_fields_listed():
    rpc = GateRpc()
    row = candidate()
    row["request_at_proposed_pin"]["unknownImportantField"] = 7
    unknown = collect_increase_sidecar(
        rpc, deployment(), row, reader_address=READER,
        reader_code_hash=CODE_HASH, index_token=TOKEN,
        long_token=TOKEN, short_token=STORE)
    assert "unknownImportantField" in unknown["failure"]["reason"]
    assert rpc.calls == []
    valid = collect(GateRpc())
    assert "swapPath" in valid["archive_only_request_fields"]


def test_initial_and_final_block_number_mismatch_fail_closed():
    initial = collect(GateRpc(wrong_number=True))
    assert initial["status"] == "unavailable"
    assert "number mismatch" in initial["failure"]["reason"]
    final = collect(GateRpc(late_wrong_number=True))
    assert final["status"] == "unavailable"
    assert "post-read block hash unavailable" in final["failure"]["reason"]


def test_reader_timestamp_fields_require_exact_match_after_ui_fee_decode():
    from unittest.mock import patch
    import gmx_crypto_bot_v2.crosscheck.archive_state as state

    expected = {"account": ROUTER, "market": STORE,
                "initialCollateralToken": STORE, "isLong": True,
                "orderType": 2, "updatedAtTime": 1789932722,
                "validFromTime": 0, "uiFeeFactor": 17, "sizeDeltaUsd": 100}
    decoded = expected.copy()
    empty = {"sizeInUsd": 0, "sizeInTokens": 0, "collateralAmount": 0,
             "pendingImpactAmount": 0, "borrowingFactor": 0,
             "fundingFeeAmountPerSize": 0}

    class ReaderRpc(GateRpc):
        def request(self, method, params):
            if method == "eth_call":
                assert params[0]["to"] == READER and params[1] == "0x64"
                return {"result": "0x" + "00" * 32}
            return super().request(method, params)

    with patch.object(state, "_decode_order", return_value=decoded.copy()), \
            patch.object(state, "_decode_position", return_value=empty):
        order_position = PinnedArchiveStateReader(ReaderRpc(), deployment()).read_order_position(
            block=100, expected_hash=HASH, order_key=KEY,
            expected_request=expected, reader_address=READER,
            reader_code_hash=CODE_HASH)
    assert order_position.order["updatedAtTime"] == 1789932722
    assert order_position.order["validFromTime"] == 0
    assert order_position.reader_timestamp_layout == "order_numbers_13_with_ui_fee_factor"
    with patch.object(state, "_decode_order", return_value={**decoded, "updatedAtTime": 0,
                                                            "validFromTime": 1789932722}), \
            patch.object(state, "_decode_position", return_value=empty):
        try:
            PinnedArchiveStateReader(ReaderRpc(), deployment()).read_order_position(
                block=100, expected_hash=HASH, order_key=KEY,
                expected_request=expected, reader_address=READER,
                reader_code_hash=CODE_HASH)
        except ValueError as error:
            assert "differs" in str(error)
        else:
            raise AssertionError("timestamp mismatch must fail closed")
