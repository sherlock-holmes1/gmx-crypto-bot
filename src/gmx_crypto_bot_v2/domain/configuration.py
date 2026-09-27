"""GMX fee storage-key descriptions."""

from __future__ import annotations

from dataclasses import dataclass

from gmx_crypto_bot_v2.domain.keys import (
    config_base_key,
    config_market_data,
    config_market_side_data,
    keccak256,
)

ZERO_ADDRESS = "0x" + "0" * 40


PRECISION = 10**30


@dataclass(frozen=True)
class ConfigKey:
    name: str
    data: str = "0x"
    pinned_field: str | None = None

    @property
    def base_key(self) -> str:
        return config_base_key(self.name)

    @property
    def storage_key(self) -> str:
        if self.data == "0x":
            return self.base_key
        return "0x" + keccak256(bytes.fromhex(self.base_key[2:] + self.data[2:])).hex()


def fee_keys(market: str, receivers: set[str]) -> dict[str, ConfigKey]:
    keys = {
        "position_fee_positive": ConfigKey(
            "POSITION_FEE_FACTOR",
            config_market_side_data(market, True),
            "position_fee_factor_for_balance_was_improved",
        ),
        "position_fee_negative": ConfigKey(
            "POSITION_FEE_FACTOR",
            config_market_side_data(market, False),
            "position_fee_factor_for_balance_was_not_improved",
        ),
        "position_fee_receiver": ConfigKey("POSITION_FEE_RECEIVER_FACTOR"),
        "borrowing_fee_receiver": ConfigKey("BORROWING_FEE_RECEIVER_FACTOR"),
        "max_ui_fee": ConfigKey("MAX_UI_FEE_FACTOR"),
    }
    for receiver in sorted(receivers - {ZERO_ADDRESS}):
        keys["ui_fee:" + receiver] = ConfigKey(
            "UI_FEE_FACTOR", config_market_data(receiver)
        )
    return keys
