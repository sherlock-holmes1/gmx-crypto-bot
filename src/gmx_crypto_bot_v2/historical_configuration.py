"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.checks.fees import (
    compare_historical_fees as compare_historical_fees,
)
from gmx_crypto_bot_v2.domain.configuration import PRECISION as PRECISION
from gmx_crypto_bot_v2.domain.configuration import ZERO_ADDRESS as ZERO_ADDRESS
from gmx_crypto_bot_v2.domain.configuration import ConfigKey as ConfigKey
from gmx_crypto_bot_v2.domain.configuration import fee_keys as fee_keys
from gmx_crypto_bot_v2.evidence.anchors import (
    _recorded_block_hash as _recorded_block_hash,
)
from gmx_crypto_bot_v2.reconstruction.fees import ConfigHistory as ConfigHistory
from gmx_crypto_bot_v2.reconstruction.fees import (
    build_fee_histories as build_fee_histories,
)
