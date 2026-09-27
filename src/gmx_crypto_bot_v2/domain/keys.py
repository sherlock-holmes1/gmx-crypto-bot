"""Ethereum Keccak and GMX configuration-key encoding."""

from __future__ import annotations

FACTOR_FIELDS = {
    ("POSITION_IMPACT_FACTOR", True): "position_impact_factor_positive",
    ("POSITION_IMPACT_FACTOR", False): "position_impact_factor_negative",
    (
        "POSITION_IMPACT_EXPONENT_FACTOR",
        True,
    ): "position_impact_exponent_factor_positive",
    (
        "POSITION_IMPACT_EXPONENT_FACTOR",
        False,
    ): "position_impact_exponent_factor_negative",
    ("MAX_POSITION_IMPACT_FACTOR", True): "max_position_impact_factor_positive",
    ("MAX_POSITION_IMPACT_FACTOR", False): "max_position_impact_factor_negative",
}


MARKET_FIELDS = {
    "MAX_LENDABLE_IMPACT_FACTOR": "max_lendable_impact_factor",
    "MAX_LENDABLE_IMPACT_USD": "max_lendable_impact_usd",
}


MASK = (1 << 64) - 1


RHO = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


ROUNDS = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)


def keccak256(data: bytes) -> bytes:
    """Ethereum Keccak-256, with legacy 0x01 domain padding."""
    padded = bytearray(data) + b"\x01"
    padded.extend(bytes((-len(padded)) % 136))
    padded[-1] |= 0x80
    state = [0] * 25
    for start in range(0, len(padded), 136):
        for lane in range(17):
            state[lane] ^= int.from_bytes(
                padded[start + lane * 8 : start + lane * 8 + 8], "little"
            )
        for constant in ROUNDS:
            parity = [
                state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
                for x in range(5)
            ]
            for x in range(5):
                neighbor = parity[(x + 1) % 5]
                delta = parity[(x - 1) % 5] ^ ((neighbor << 1 | neighbor >> 63) & MASK)
                for y in range(5):
                    state[x + 5 * y] ^= delta
            rotated = [0] * 25
            for x in range(5):
                for y in range(5):
                    value, shift = state[x + 5 * y], RHO[x][y]
                    rotated[y + 5 * ((2 * x + 3 * y) % 5)] = (
                        ((value << shift) | (value >> (64 - shift))) & MASK
                        if shift
                        else value
                    )
            for x in range(5):
                for y in range(5):
                    state[x + 5 * y] = rotated[x + 5 * y] ^ (
                        ~rotated[(x + 1) % 5 + 5 * y] & rotated[(x + 2) % 5 + 5 * y]
                    )
            state[0] ^= constant
    return b"".join(value.to_bytes(8, "little") for value in state)[:32]


def config_base_key(name: str) -> str:
    encoded = name.encode()
    payload = (
        (32).to_bytes(32, "big")
        + len(encoded).to_bytes(32, "big")
        + encoded
        + bytes((-len(encoded)) % 32)
    )
    return "0x" + keccak256(payload).hex()


def config_market_side_data(market: str, positive: bool) -> str:
    return (
        "0x"
        + int(market, 16).to_bytes(32, "big").hex()
        + int(positive).to_bytes(32, "big").hex()
    )


def config_market_data(market: str) -> str:
    return "0x" + int(market, 16).to_bytes(32, "big").hex()
