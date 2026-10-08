from gmx_crypto_bot_v2.crosscheck.deployment_prep import prepare_deployment
from gmx_crypto_bot_v2.domain.keys import keccak256


HASH = "0x" + "aa" * 32
STORE = "0x" + "11" * 20
READER = "0x" + "22" * 20
HANDLER = "0x" + "33" * 20
ROUTER = "0x" + "44" * 20
REFERRAL = "0x" + "55" * 20
CODE = "0x6000"
CODE_HASH = "0x" + keccak256(bytes.fromhex(CODE[2:])).hex()


def candidate():
    return {"recording": "recordings/example", "order_key": "0x" + "55" * 32,
            "proposed_pin_block": 100, "proposed_pin_hash": HASH,
            "creation": {"block_number": 100}, "selection_skip_reasons": []}


class Rpc:
    def __init__(self, *, wrong_number=False, reorg=False, empty_code=False,
                 wrong_referral_pointer=False):
        self.calls = []
        self.headers = 0
        self.wrong_number = wrong_number
        self.reorg = reorg
        self.empty_code = empty_code
        self.wrong_referral_pointer = wrong_referral_pointer

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return {"result": "0xa4b1"}
        if method == "eth_getBlockByNumber":
            self.headers += 1
            assert params == ["0x64", False]
            return {"result": {"number": "0x65" if self.wrong_number else "0x64",
                               "hash": "0x" + "bb" * 32 if self.reorg and self.headers > 1
                               else HASH}}
        if method == "eth_getCode":
            assert params[0] in (STORE, READER, HANDLER, ROUTER, REFERRAL)
            assert params[1] == "0x64"
            return {"result": "0x" if self.empty_code else CODE}
        if method == "eth_call":
            assert params[0]["to"] == HANDLER and params[1] == "0x64"
            return {"result": "0x" + f"{int(HANDLER if self.wrong_referral_pointer else REFERRAL, 16):064x}"}
        raise AssertionError(method)


def prepare(rpc, row=None):
    return prepare_deployment(rpc, candidate() if row is None else row,
                              chain_id=42161, datastore=STORE, reader=READER,
                              order_handler=HANDLER, router=ROUTER)


def test_observed_hashes_are_sidecar_compatible_and_unverified():
    rpc = Rpc()
    result = prepare(rpc)
    assert result["deployment"]["router_code_hash"] == CODE_HASH
    assert result["deployment"]["datastore_code_hash"] == CODE_HASH
    assert result["reader"]["code_hash"] == CODE_HASH
    assert result["order_handler"]["code_hash"] == CODE_HASH
    assert result["proof_level"] == "code_hashes_observed_at_pin_not_source_verified"
    assert result["ready_for_comparison"] is False
    assert len([method for method, _ in rpc.calls if method == "eth_getCode"]) == 4
    assert not any(method in ("eth_sendTransaction", "eth_sendRawTransaction")
                   for method, _ in rpc.calls)


def test_mismatched_header_empty_code_and_reorg_fail():
    for rpc, reason in ((Rpc(wrong_number=True), "number mismatch"),
                        (Rpc(empty_code=True), "missing historical"),
                        (Rpc(reorg=True), "block changed")):
        try:
            prepare(rpc)
        except ValueError as error:
            assert reason in str(error)
        else:
            raise AssertionError("expected fail-closed deployment preparation")


def test_recorded_hash_mismatch_stops_before_code_read():
    rpc = Rpc()
    row = candidate()
    row["proposed_pin_hash"] = "0x" + "ff" * 32
    try:
        prepare(rpc, row)
    except ValueError as error:
        assert "recorded archive block hash mismatch" in str(error)
    else:
        raise AssertionError("expected archive hash failure")
    assert not any(method == "eth_getCode" for method, _ in rpc.calls)


def test_referral_address_is_explicit_and_checked_against_handler_pointer():
    rpc = Rpc()
    result = prepare_deployment(rpc, candidate(), chain_id=42161,
                                datastore=STORE, reader=READER,
                                order_handler=HANDLER, router=ROUTER,
                                referral_storage=REFERRAL)
    assert result["referral_storage"] == {"address": REFERRAL, "code_hash": CODE_HASH}
    assert any(method == "eth_call" for method, _ in rpc.calls)
    try:
        prepare_deployment(Rpc(wrong_referral_pointer=True), candidate(), chain_id=42161,
                           datastore=STORE, reader=READER,
                           order_handler=HANDLER, router=ROUTER,
                           referral_storage=REFERRAL)
    except ValueError as error:
        assert "pointer differs" in str(error)
    else:
        raise AssertionError("expected referral pointer mismatch")
