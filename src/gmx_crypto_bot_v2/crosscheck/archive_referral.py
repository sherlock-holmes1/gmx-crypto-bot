"""Pinned referral and pro-tier inputs for one GMX order account."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.crosscheck.archive_state import Cell, PinnedArchiveStateReader
from gmx_crypto_bot_v2.crosscheck.router_preflight import Deployment, RawRpc, _hex_bytes
from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.domain.referral import ZERO, ZERO_CODE, calldata, datastore_key, words


def read_referral_at_pin(
    rpc: RawRpc, deployment: Deployment, *, block: int, expected_hash: str,
    account: str, order_handler: str, order_handler_code_hash: str,
    referral_storage: str, referral_storage_code_hash: str,
) -> dict[str, Any]:
    """Read exact referral dependencies at a block boundary; fail on missing proof.

    The caller supplies a historical ReferralStorage address/hash. The pinned
    OrderHandler pointer must agree. Source/ABI identity remains a separate
    evidence requirement even if all calls return well-formed values.
    """
    reader = PinnedArchiveStateReader(rpc, deployment)
    if type(block) is not int or block < 0:
        raise ValueError("invalid referral pin block")
    expected_hash = "0x" + _hex_bytes(expected_hash, 32).hex()
    account = "0x" + _hex_bytes(account, 20).hex()
    order_handler = "0x" + _hex_bytes(order_handler, 20).hex()
    referral_storage = "0x" + _hex_bytes(referral_storage, 20).hex()
    if referral_storage == ZERO:
        raise ValueError("zero referral storage address")
    for expected in (order_handler_code_hash, referral_storage_code_hash):
        _hex_bytes(expected, 32)
    if int(reader._rpc("eth_chainId", []), 16) != deployment.chain_id:
        raise ValueError("referral archive chain mismatch")
    if reader._block_hash(block) != expected_hash:
        raise ValueError("referral archive block mismatch")
    for address, expected in ((order_handler, order_handler_code_hash),
                              (referral_storage, referral_storage_code_hash)):
        code = reader._rpc("eth_getCode", [address, hex(block)])
        if not isinstance(code, str) or not code.startswith("0x") or code == "0x":
            raise ValueError("missing referral deployment code")
        if "0x" + keccak256(bytes.fromhex(code[2:])).hex() != expected.lower():
            raise ValueError("referral deployment code hash mismatch")

    def call(to: str, signature: str, argument: str | int | None = None,
             count: int = 1) -> list[int]:
        raw = reader._rpc("eth_call", [{"to": to, "data": calldata(signature, argument)},
                                       hex(block)])
        return words(raw, count)

    pointer = call(order_handler, "referralStorage()")[0]
    if pointer >= 2**160 or "0x" + f"{pointer:040x}" != referral_storage:
        raise ValueError("OrderHandler referral pointer mismatch")
    code = "0x" + f"{call(referral_storage, 'traderReferralCodes(address)', account)[0]:064x}"
    pro_tier_key = datastore_key("PRO_TRADER_TIER", account)
    pro_tier = reader.read(block, expected_hash,
                           (Cell("referral", "pro_trader_tier", "Uint", pro_tier_key),)
                           ).values["referral"]["pro_trader_tier"]
    result: dict[str, Any] = {
        "storage": referral_storage, "storage_code_hash": referral_storage_code_hash,
        "handler_pointer": referral_storage, "account": account,
        "code": code, "pro_trader_tier": pro_tier,
        "affiliate": ZERO, "referral_tier": 0, "total_rebate_bps": 0,
        "discount_share_bps": 0, "custom_discount_share_bps": 0,
        "minimum_affiliate_reward_factor": 0, "pro_discount_factor": 0,
        "datastore_keys": {"pro_trader_tier": pro_tier_key},
        "source_and_abi_verified": False,
    }
    if code != ZERO_CODE:
        owner = call(referral_storage, "codeOwners(bytes32)", code)[0]
        if owner == 0 or owner >= 2**160:
            raise ValueError("invalid referral affiliate")
        affiliate = "0x" + f"{owner:040x}"
        tier = call(referral_storage, "referrerTiers(address)", affiliate)[0]
        rebate, share = call(referral_storage, "tiers(uint256)", tier, 2)
        custom = call(referral_storage, "referrerDiscountShares(address)", affiliate)[0]
        if rebate > 10000 or share > 10000 or custom > 10000:
            raise ValueError("invalid referral basis points")
        minimum_key = datastore_key("MIN_AFFILIATE_REWARD_FACTOR", tier)
        minimum = reader.read(block, expected_hash,
                              (Cell("referral", "minimum_affiliate_reward_factor", "Uint",
                                    minimum_key),)).values["referral"]["minimum_affiliate_reward_factor"]
        result.update(affiliate=affiliate, referral_tier=tier,
                      total_rebate_bps=rebate,
                      discount_share_bps=custom or share,
                      custom_discount_share_bps=custom,
                      minimum_affiliate_reward_factor=minimum)
        result["datastore_keys"]["minimum_affiliate_reward_factor"] = minimum_key
    if pro_tier:
        pro_key = datastore_key("PRO_DISCOUNT_FACTOR", pro_tier)
        pro_factor = reader.read(block, expected_hash,
                                 (Cell("referral", "pro_discount_factor", "Uint", pro_key),)
                                 ).values["referral"]["pro_discount_factor"]
        result["pro_discount_factor"] = pro_factor
        result["datastore_keys"]["pro_discount_factor"] = pro_key
    if reader._block_hash(block) != expected_hash:
        raise ValueError("referral archive block changed")
    return result
