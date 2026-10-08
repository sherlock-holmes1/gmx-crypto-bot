from gmx_crypto_bot_v2.crosscheck.archive_referral import read_referral_at_pin
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, _selector
from gmx_crypto_bot_v2.domain.keys import keccak256


BLOCK_HASH = "0x" + "aa" * 32
STORE = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
HANDLER = "0x" + "33" * 20
REFERRAL = "0x" + "44" * 20
ACCOUNT = "0x" + "55" * 20
AFFILIATE = "0x" + "66" * 20
CODE = "0x6000"
CODE_HASH = "0x" + keccak256(bytes.fromhex(CODE[2:])).hex()


def word(value):
    return "0x" + f"{value:064x}"


class Rpc:
    def __init__(self, *, referral_code=0, wrong_pointer=False, reorg=False):
        self.referral_code = referral_code
        self.wrong_pointer = wrong_pointer
        self.reorg = reorg
        self.headers = 0
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return {"result": "0x1"}
        if method == "eth_getBlockByNumber":
            self.headers += 1
            return {"result": {"number": "0x64", "hash":
                               "0x" + "bb" * 32 if self.reorg and self.headers > 1
                               else BLOCK_HASH}}
        if method == "eth_getCode":
            assert params[1] == "0x64"
            return {"result": CODE}
        if method == "eth_call":
            to, data = params[0]["to"], params[0]["data"]
            assert params[1] == "0x64"
            if to == HANDLER:
                assert data[:10] == "0x" + _selector("referralStorage()").hex()
                return {"result": word(int(HANDLER if self.wrong_pointer else REFERRAL, 16))}
            if to == REFERRAL:
                selector = data[:10]
                if selector == "0x" + _selector("traderReferralCodes(address)").hex():
                    return {"result": word(self.referral_code)}
                if selector == "0x" + _selector("codeOwners(bytes32)").hex():
                    return {"result": word(int(AFFILIATE, 16))}
                if selector == "0x" + _selector("referrerTiers(address)").hex():
                    return {"result": word(2)}
                if selector == "0x" + _selector("tiers(uint256)").hex():
                    return {"result": word(1000) + word(5000)[2:]}
                if selector == "0x" + _selector("referrerDiscountShares(address)").hex():
                    return {"result": word(0)}
            if to == STORE:
                return {"result": word(3 if self.referral_code else 0)}
        raise AssertionError((method, params))


def run(rpc):
    deployment = Deployment(1, ROUTER, CODE_HASH, STORE, CODE_HASH,
                            ("EndOfOracleSimulation()",))
    return read_referral_at_pin(
        rpc, deployment, block=100, expected_hash=BLOCK_HASH,
        account=ACCOUNT, order_handler=HANDLER,
        order_handler_code_hash=CODE_HASH,
        referral_storage=REFERRAL, referral_storage_code_hash=CODE_HASH)


def test_zero_code_still_reads_pro_tier_and_pinned_pointer():
    rpc = Rpc()
    result = run(rpc)
    assert result["code"] == "0x" + "00" * 32
    assert result["pro_trader_tier"] == 0
    assert result["affiliate"] == "0x" + "00" * 20
    assert result["source_and_abi_verified"] is False
    assert not any(method.startswith("eth_send") for method, _ in rpc.calls)


def test_nonzero_referral_reads_affiliate_tier_and_datastore_terms():
    result = run(Rpc(referral_code=7))
    assert result["code"].endswith("07")
    assert result["affiliate"] == AFFILIATE
    assert result["referral_tier"] == 2
    assert result["total_rebate_bps"] == 1000
    assert result["discount_share_bps"] == 5000
    assert "minimum_affiliate_reward_factor" in result["datastore_keys"]
    assert "pro_discount_factor" in result["datastore_keys"]


def test_wrong_pointer_and_reorg_fail_closed():
    for rpc, reason in ((Rpc(wrong_pointer=True), "pointer mismatch"),
                        (Rpc(reorg=True), "block hash mismatch")):
        try:
            run(rpc)
        except ValueError as error:
            assert reason in str(error)
        else:
            raise AssertionError("expected pinned referral failure")
