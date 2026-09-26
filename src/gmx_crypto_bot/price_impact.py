"""Independent GMX position-impact curve primitives."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import json
from decimal import Decimal, localcontext
from pathlib import Path

from gmx_crypto_bot.event_decoder import decode_event_log, event_name_from_data

FLOAT_PRECISION = 10**30
FACTOR_FIELDS = {
    ("POSITION_IMPACT_FACTOR", True): "position_impact_factor_positive",
    ("POSITION_IMPACT_FACTOR", False): "position_impact_factor_negative",
    ("POSITION_IMPACT_EXPONENT_FACTOR", True): "position_impact_exponent_factor_positive",
    ("POSITION_IMPACT_EXPONENT_FACTOR", False): "position_impact_exponent_factor_negative",
    ("MAX_POSITION_IMPACT_FACTOR", True): "max_position_impact_factor_positive",
    ("MAX_POSITION_IMPACT_FACTOR", False): "max_position_impact_factor_negative",
}
MARKET_FIELDS = {
    "MAX_LENDABLE_IMPACT_FACTOR": "max_lendable_impact_factor",
    "MAX_LENDABLE_IMPACT_USD": "max_lendable_impact_usd",
}
MASK = (1 << 64) - 1
RHO = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))
ROUNDS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)


def keccak256(data: bytes) -> bytes:
    """Ethereum Keccak-256, with legacy 0x01 domain padding."""
    padded = bytearray(data) + b"\x01"
    padded.extend(bytes((-len(padded)) % 136))
    padded[-1] |= 0x80
    state = [0] * 25
    for start in range(0, len(padded), 136):
        for lane in range(17):
            state[lane] ^= int.from_bytes(padded[start + lane * 8:start + lane * 8 + 8], "little")
        for constant in ROUNDS:
            parity = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
            for x in range(5):
                neighbor = parity[(x + 1) % 5]
                delta = parity[(x - 1) % 5] ^ ((neighbor << 1 | neighbor >> 63) & MASK)
                for y in range(5):
                    state[x + 5 * y] ^= delta
            rotated = [0] * 25
            for x in range(5):
                for y in range(5):
                    value, shift = state[x + 5 * y], RHO[x][y]
                    rotated[y + 5 * ((2 * x + 3 * y) % 5)] = ((value << shift) | (value >> (64 - shift))) & MASK if shift else value
            for x in range(5):
                for y in range(5):
                    state[x + 5 * y] = rotated[x + 5 * y] ^ (~rotated[(x + 1) % 5 + 5 * y] & rotated[(x + 2) % 5 + 5 * y])
            state[0] ^= constant
    return b"".join(value.to_bytes(8, "little") for value in state)[:32]


def config_base_key(name: str) -> str:
    encoded = name.encode()
    payload = (32).to_bytes(32, "big") + len(encoded).to_bytes(32, "big") + encoded + bytes((-len(encoded)) % 32)
    return "0x" + keccak256(payload).hex()


def config_market_side_data(market: str, positive: bool) -> str:
    return "0x" + int(market, 16).to_bytes(32, "big").hex() + int(positive).to_bytes(32, "big").hex()


def config_market_data(market: str) -> str:
    return "0x" + int(market, 16).to_bytes(32, "big").hex()


@dataclass(frozen=True)
class HistoricalFactor:
    """A terminal snapshot plus all recorded changes to one GMX uint key."""

    anchor_block: int
    anchor_value: int
    changes: tuple[tuple[tuple[int, int, int], int], ...]
    opening_value: int | None = None

    def __post_init__(self) -> None:
        if tuple(sorted(self.changes)) != self.changes:
            raise ValueError("configuration changes must be in canonical order")
        if self.changes and self.changes[-1][0][0] > self.anchor_block:
            raise ValueError("configuration change occurs after anchor")
        if self.changes and self.changes[-1][1] != self.anchor_value:
            raise ValueError("final recorded configuration disagrees with anchor")
        if not self.changes and self.opening_value is not None and self.opening_value != self.anchor_value:
            raise ValueError("unchanged configuration disagrees with opening snapshot")

    def at(self, coordinate: tuple[int, int, int]) -> int | None:
        """Return the effective value, or None before the first observed write."""
        if coordinate[0] > self.anchor_block:
            return None
        locations = [location for location, _ in self.changes]
        index = bisect_right(locations, coordinate) - 1
        if index >= 0:
            return self.changes[index][1]
        if self.opening_value is not None:
            return self.opening_value
        if not self.changes:
            return self.anchor_value
        return None


def load_factor_histories(recording: Path, metadata: dict) -> dict[str, HistoricalFactor]:
    """Version impact factors from recorded writes and the pinned terminal anchor."""
    anchor = metadata.get("pinned_configuration_anchor_block")
    pinned = metadata.get("pinned_configuration_raw", {})
    market = metadata["market"]["market_token_address"].lower()
    if not isinstance(anchor, int):
        return {}
    wanted = {
        (config_base_key(name), config_market_side_data(market, positive)): field
        for (name, positive), field in FACTOR_FIELDS.items()
    }
    wanted.update({
        (config_base_key(name), config_market_data(market)): field
        for name, field in MARKET_FIELDS.items()
    })
    changes: dict[str, list[tuple[tuple[int, int, int], int]]] = {field: [] for field in wanted.values()}
    checkpoint: dict | None = None
    with (recording / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item.get("kind") == "opening_state_checkpoint":
                checkpoint = item["payload"]
            log = item.get("payload", {}).get("log")
            if not log or event_name_from_data(log.get("data", "")) != "SetUint":
                continue
            values = decode_event_log(log["data"]).values
            field = wanted.get((values.get("baseKey"), values.get("data")))
            if field is None:
                continue
            coordinate = (item["block_number"], item["transaction_index"], item["log_index"])
            if coordinate[0] > anchor:
                raise ValueError("position-impact configuration write occurs after pinned anchor")
            changes[field].append((coordinate, values["value"]))
    opening_values: dict[str, int] = {}
    sidecar = recording / "impact-opening-configuration.json"
    if sidecar.exists():
        snapshot = json.loads(sidecar.read_text(encoding="utf-8"))
        if (checkpoint is None or snapshot.get("schema") != "GmxImpactOpeningConfiguration"
                or snapshot.get("version") != 1
                or snapshot.get("block_number") != checkpoint.get("block_number")
                or snapshot.get("block_hash") != checkpoint.get("block_hash")
                or snapshot.get("market") != market):
            raise ValueError("impact opening configuration does not match recording checkpoint")
        opening_values = {field: int(value) for field, value in snapshot.get("factors", {}).items()}
    return {
        field: HistoricalFactor(anchor, int(pinned[field]), tuple(sorted(events)), opening_values.get(field))
        for field, events in changes.items() if field in pinned
    }


def balance_impact(
    long_interest: int, short_interest: int, signed_delta: int, is_long: bool,
    positive_factor: int, negative_factor: int, positive_exponent: int, negative_exponent: int,
    apply_exponent,
) -> tuple[int, bool]:
    """Raw OI-balance impact, before virtual-inventory and pool caps."""
    next_side = max(0, (long_interest if is_long else short_interest) + signed_delta)
    next_long, next_short = (next_side, short_interest) if is_long else (long_interest, next_side)
    initial_diff, next_diff = abs(long_interest - short_interest), abs(next_long - next_short)
    improved = next_diff < initial_diff
    positive_factor = min(positive_factor, negative_factor)
    positive_exponent = min(positive_exponent, negative_exponent)

    def adjusted(diff: int, factor: int, exponent: int) -> int:
        return apply_exponent(diff, exponent) * factor // FLOAT_PRECISION

    same_side = (long_interest <= short_interest) == (next_long <= next_short)
    if same_side:
        factor = positive_factor if improved else negative_factor
        exponent = positive_exponent if improved else negative_exponent
        initial, final = adjusted(initial_diff, factor, exponent), adjusted(next_diff, factor, exponent)
        return (abs(initial - final) if improved else -abs(initial - final)), improved
    initial = adjusted(initial_diff, positive_factor, positive_exponent)
    final = adjusted(next_diff, negative_factor, negative_exponent)
    return initial - final, improved


def apply_exponent_factor(value: int, exponent_factor: int) -> int:
    """GMX 30-decimal to 18-decimal pow bridge, with Decimal approximation.

    PRBMathUD60x18's final few wei can differ; the validator uses a fixed
    USD-integer tolerance for this comparison, never exact equality.
    """
    if value < FLOAT_PRECISION:
        return 0
    if exponent_factor == FLOAT_PRECISION:
        return value
    with localcontext() as context:
        context.prec = 80
        base = Decimal(value // 10**12) / 10**18
        exponent = Decimal(exponent_factor // 10**12) / 10**18
        return int(base ** exponent * 10**18) * 10**12


def predict_current_impact(
    long_interest_tokens: int, short_interest_tokens: int,
    virtual_inventory_tokens: int, token_delta: int, is_long: bool,
    index_min: int, index_max: int, factors: dict[str, int],
) -> dict[str, int | bool]:
    """Calculate GMX's OI and virtual-inventory curves before settlement caps."""
    mid = (index_min + index_max) // 2
    signed_usd_delta = token_delta * mid
    args = (
        factors["position_impact_factor_positive"],
        factors["position_impact_factor_negative"],
        factors["position_impact_exponent_factor_positive"],
        factors["position_impact_exponent_factor_negative"],
        apply_exponent_factor,
    )
    market_impact, improved = balance_impact(
        long_interest_tokens * mid, short_interest_tokens * mid,
        signed_usd_delta, is_long, *args,
    )
    virtual_impact = market_impact
    if market_impact < 0:
        virtual_usd = virtual_inventory_tokens * mid
        virtual_long = max(-virtual_usd, 0)
        virtual_short = max(virtual_usd, 0)
        if signed_usd_delta < 0:
            virtual_long -= signed_usd_delta
            virtual_short -= signed_usd_delta
        virtual_impact, virtual_improved = balance_impact(
            virtual_long, virtual_short, signed_usd_delta, is_long, *args,
        )
        if virtual_impact < market_impact:
            improved = virtual_improved
    selected = min(market_impact, virtual_impact)
    return {
        "market_impact_usd": market_impact,
        "virtual_impact_usd": virtual_impact,
        "selected_impact_usd": selected,
        "balance_was_improved": improved,
    }
