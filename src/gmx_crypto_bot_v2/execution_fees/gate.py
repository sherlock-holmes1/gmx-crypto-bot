"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.checks.execution import CHECK as CHECK
from gmx_crypto_bot_v2.checks.execution import (
    apply_execution_fee_proof as apply_evidence_proof,
)


def apply_execution_fee_proof(recording, orders):
    from gmx_crypto_bot_v2.evidence.traces import TraceRepository

    return apply_evidence_proof(TraceRepository(recording), orders)
