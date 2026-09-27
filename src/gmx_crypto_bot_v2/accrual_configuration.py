"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.domain.accrual import BOOL_CONFIG as BOOL_CONFIG
from gmx_crypto_bot_v2.domain.accrual import BORROWING_CONFIG as BORROWING_CONFIG
from gmx_crypto_bot_v2.domain.accrual import FUNDING_CONFIG as FUNDING_CONFIG
from gmx_crypto_bot_v2.domain.accrual import decode as decode
from gmx_crypto_bot_v2.domain.accrual import slots as slots
from gmx_crypto_bot_v2.reconstruction.accrual_configuration import (
    load_snapshot as load_snapshot,
)
