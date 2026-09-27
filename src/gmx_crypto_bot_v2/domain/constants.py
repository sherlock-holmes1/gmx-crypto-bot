"""Order types, event families, precision and deployment constants."""

from __future__ import annotations

TERMINAL_EVENTS = {"OrderExecuted", "OrderCancelled", "OrderFrozen"}


LIFECYCLE_EVENTS = TERMINAL_EVENTS | {
    "OrderUpdated",
    "OrderSizeDeltaAutoUpdated",
    "OrderCollateralDeltaAmountAutoUpdated",
}


POSITION_EVENTS = {"PositionIncrease", "PositionDecrease", "PositionFeesCollected"}


INCREASE_ORDER_TYPES = {2, 3, 8}


DECREASE_ORDER_TYPES = {4, 5, 6, 7}


MAX_UINT256 = (1 << 256) - 1


FLOAT_PRECISION = 10**30


FUNDING_PRECISION = 10**45


IMPACT_ROUNDING_TOLERANCE_USD = 10**18  # $1e-12 in GMX's 30-decimal USD units


ARBITRUM_ORDER_VAULT = "0x31ef83a530fde1b38ee9a18093a333d8bbbc40d5"


ARBITRUM_MULTICHAIN_VAULT = "0xceaadfaf6a8c489b250e407987877c5fdfcdbe6e"


ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


UNMODELED_ECONOMICS = (
    "historical_configuration_factors",
    "execution_fee_gas_and_transfer_proof",
    "liquidation_settlement",
)
