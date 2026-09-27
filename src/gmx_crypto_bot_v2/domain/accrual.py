"""GMX funding and borrowing storage-key inventories."""

from gmx_crypto_bot_v2.domain.swap_keys import key

FUNDING_CONFIG = (
    "FUNDING_FACTOR",
    "FUNDING_EXPONENT_FACTOR",
    "FUNDING_INCREASE_FACTOR_PER_SECOND",
    "FUNDING_DECREASE_FACTOR_PER_SECOND",
    "MIN_FUNDING_FACTOR_PER_SECOND",
    "MAX_FUNDING_FACTOR_PER_SECOND",
    "THRESHOLD_FOR_STABLE_FUNDING",
    "THRESHOLD_FOR_DECREASE_FUNDING",
)


BORROWING_CONFIG = (
    "BORROWING_FACTOR",
    "BORROWING_EXPONENT_FACTOR",
    "OPTIMAL_USAGE_FACTOR",
    "BASE_BORROWING_FACTOR",
    "ABOVE_OPTIMAL_USAGE_BORROWING_FACTOR",
    "OPEN_INTEREST_RESERVE_FACTOR",
)


BOOL_CONFIG = (
    "SKIP_BORROWING_FEE_FOR_SMALLER_SIDE",
    "USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE",
)


def slots(metadata):
    market = metadata["market"]["market_token_address"].lower()
    tokens = [metadata["tokens"][t]["address"].lower() for t in ("long", "short")]
    if tokens[0] == tokens[1]:
        raise ValueError("single-token accrual market unsupported")
    result = {}

    def add(kind, name, *args):
        result[key(name, *args)] = {"kind": kind, "name": name, "args": list(args)}

    for name in FUNDING_CONFIG + ("LIQUIDATION_FEE_FACTOR",):
        add("Uint", name, market)
    add("Uint", "LIQUIDATION_FEE_RECEIVER_FACTOR")
    for name in BOOL_CONFIG:
        add("Bool", name)
    add("Int", "SAVED_FUNDING_FACTOR_PER_SECOND", market)
    add("Uint", "FUNDING_UPDATED_AT", market)
    for side in (True, False):
        for name in BORROWING_CONFIG + (
            "CUMULATIVE_BORROWING_FACTOR",
            "CUMULATIVE_BORROWING_FACTOR_UPDATED_AT",
        ):
            add("Uint", name, market, side)
        for token in tokens:
            for name in (
                "OPEN_INTEREST",
                "OPEN_INTEREST_IN_TOKENS",
                "FUNDING_FEE_AMOUNT_PER_SIZE",
                "CLAIMABLE_FUNDING_AMOUNT_PER_SIZE",
            ):
                add("Uint", name, market, token, side)
    for token in tokens:
        add("Uint", "POOL_AMOUNT", market, token)
    return result


def decode(raw, kind):
    if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
        raise ValueError("invalid archive ABI word")
    value = int(raw, 16)
    if kind == "Int" and value >= 2**255:
        value -= 2**256
    if kind == "Bool" and value not in (0, 1):
        raise ValueError("invalid archive boolean")
    return value
