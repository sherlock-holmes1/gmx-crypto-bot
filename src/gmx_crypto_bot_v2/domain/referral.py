"""Referral ABI encoding, log decoding and storage-key identifiers."""

from __future__ import annotations

P = 10**30

SIGNATURES = {
    "SetTraderReferralCode(address,bytes32)": ("trader", ("address", "bytes32")),
    "SetTier(uint256,uint256,uint256)": ("tier", ("uint", "uint", "uint")),
    "SetReferrerTier(address,uint256)": ("affiliate_tier", ("address", "uint")),
    "SetReferrerDiscountShare(address,uint256)": ("share", ("address", "uint")),
    "RegisterCode(address,bytes32)": ("register", ("address", "bytes32")),
    "SetCodeOwner(address,address,bytes32)": (
        "owner",
        ("address", "address", "bytes32"),
    ),
    "GovSetCodeOwner(bytes32,address)": ("gov_owner", ("bytes32", "address")),
}

PRO_NAMES = {
    "PRO_TRADER_TIER": "pro_tier",
    "PRO_DISCOUNT_FACTOR": "pro_factor",
    "MIN_AFFILIATE_REWARD_FACTOR": "minimum",
}

from typing import Any

from gmx_crypto_bot_v2.domain.keys import config_base_key, keccak256

ZERO = "0x" + "0" * 40


ZERO_CODE = "0x" + "0" * 64


TOPICS = {
    "0x" + keccak256(sig.encode()).hex(): spec for sig, spec in SIGNATURES.items()
}


PRO_BASES = {config_base_key(name): field for name, field in PRO_NAMES.items()}


def word(value: int | str) -> str:
    return f"{value if isinstance(value, int) else int(value, 16):064x}"


def calldata(signature: str, arg: int | str | None = None) -> str:
    return (
        "0x"
        + keccak256(signature.encode())[:4].hex()
        + (word(arg) if arg is not None else "")
    )


def datastore_key(name: str, arg: int | str) -> str:
    return "0x" + keccak256(bytes.fromhex(config_base_key(name)[2:] + word(arg))).hex()


def words(raw: str, count: int) -> list[int]:
    if (
        not isinstance(raw, str)
        or not raw.startswith("0x")
        or len(raw) != 2 + 64 * count
    ):
        raise ValueError("invalid ABI result width")
    return [int(raw[2 + i * 64 : 2 + (i + 1) * 64], 16) for i in range(count)]


def address(value: int) -> str:
    if value < 0 or value >= 2**160:
        raise ValueError("invalid ABI address")
    return "0x" + f"{value:040x}"


def decode_referral_log(log: dict) -> tuple[str, Any, Any] | None:
    if log.get("removed"):
        raise ValueError("removed referral log")
    topics = log.get("topics", [])
    if not topics or topics[0].lower() not in TOPICS:
        return None
    if len(topics) != 1:
        raise ValueError("unexpected indexed referral event fields")
    name, types = TOPICS[topics[0].lower()]
    values = words(log["data"], len(types))
    values = [
        address(v) if t == "address" else "0x" + word(v) if t == "bytes32" else v
        for v, t in zip(values, types)
    ]
    if name == "register":
        return "code", values[1], values[0]
    if name == "owner":
        return "code", values[2], values[1]
    if name == "gov_owner":
        return "code", values[0], values[1]
    return name, values[0], tuple(values[1:]) if name == "tier" else values[1]


def config_change(entry: dict) -> tuple[str, Any, int] | None:
    if entry.get("event_name") != "SetUint":
        return None
    v = entry["values"]
    field = PRO_BASES.get(v.get("baseKey"))
    if field is None:
        return None
    arg = words(v["data"], 1)[0]
    return field, address(arg) if field == "pro_tier" else arg, v["value"]
