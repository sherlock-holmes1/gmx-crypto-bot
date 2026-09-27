"""Compatibility entry point; implementation lives in the layered V2 package."""

import json

from gmx_crypto_bot_v2.checks.trace import (
    CALIBRATED_GAS_PROFILE as CALIBRATED_GAS_PROFILE,
)
from gmx_crypto_bot_v2.checks.trace import TRANSFER_TOPIC as TRANSFER_TOPIC
from gmx_crypto_bot_v2.checks.trace import WNT_ARBITRUM as WNT_ARBITRUM
from gmx_crypto_bot_v2.checks.trace import _events as _events
from gmx_crypto_bot_v2.checks.trace import _fee_events as _fee_events
from gmx_crypto_bot_v2.checks.trace import _opcode_positions as _opcode_positions
from gmx_crypto_bot_v2.checks.trace import _receipt_transfers as _receipt_transfers
from gmx_crypto_bot_v2.checks.trace import _trace_fee_events as _trace_fee_events
from gmx_crypto_bot_v2.checks.trace import _transfer_proof as _transfer_proof
from gmx_crypto_bot_v2.checks.trace import _word_address as _word_address
from gmx_crypto_bot_v2.checks.trace import verify_trace_evidence


def verify_trace(trace_path, gas_path, orders, *, trace_record=None):

    capture = (
        trace_record if trace_record is not None else json.loads(trace_path.read_text())
    )
    gas = json.loads(gas_path.read_text()) if gas_path and gas_path.exists() else None
    return verify_trace_evidence(capture, gas, orders)
