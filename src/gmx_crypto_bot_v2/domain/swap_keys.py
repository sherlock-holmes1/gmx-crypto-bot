"""ABI calls and GMX swap storage keys, shared below the pipeline layers."""

from gmx_crypto_bot_v2.domain.keys import config_base_key, keccak256
from gmx_crypto_bot_v2.domain.referral import word

ZERO_ID = "0x" + "0" * 64


def call_data(signature, *args):
    return (
        "0x" + keccak256(signature.encode())[:4].hex() + "".join(word(x) for x in args)
    )


def key(name, *args):
    base = config_base_key(name)
    return (
        "0x" + keccak256(bytes.fromhex(base[2:] + "".join(word(x) for x in args))).hex()
        if args
        else base
    )


def market_keys(market):
    return {
        "fee_positive": key("SWAP_FEE_FACTOR", market, True),
        "fee_negative": key("SWAP_FEE_FACTOR", market, False),
        "impact_positive": key("SWAP_IMPACT_FACTOR", market, True),
        "impact_negative": key("SWAP_IMPACT_FACTOR", market, False),
        "exponent": key("SWAP_IMPACT_EXPONENT_FACTOR", market),
    }


MARKET_FIELDS = ("MARKET_TOKEN", "INDEX_TOKEN", "LONG_TOKEN", "SHORT_TOKEN")


def market_field_key(market, field):
    # MarketStoreUtils uses address first, unlike Keys' base-first layout.
    return (
        "0x" + keccak256(bytes.fromhex(word(market) + config_base_key(field)[2:])).hex()
    )
