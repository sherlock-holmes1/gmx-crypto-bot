"""Move one recorded candidate's pin to a prestate-proved pre-execution boundary.

Candidates are pinned at their creation block. For an order created in one block
and executed in a later one, that pin can never be the pre-execution boundary.
Only a verified intra-block prestate proof may move a pin, and only to the block
number and hash that proof established. No caller-chosen block is accepted.

The same helper is used by the preflight, the deployment observation, and the
archive sidecar, so every artifact in a boundary-pinned comparison is pinned by
the identical rule rather than by three independent re-implementations.
"""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.crosscheck.block_prestate import verify_system_transaction_prestate


PIN_SOURCE = "prestate_proved_pre_execution_boundary"


def verified_boundary_proof(path: Any, evidence: dict[str, Any],
                            chain_id: int) -> dict[str, Any]:
    """Rebuild a positive prestate verdict and bind it to this order's execution.

    The proof itself knows only a block coordinate. Its link to the selected
    order comes from the recorded oracle evidence, whose coordinates name the
    observed `OrderExecuted` transaction and are digest-bound to the order key.
    """
    coordinates = evidence.get("coordinates")
    if evidence.get("relationship") != \
            "observed_execution_transaction_prices_preceding_order_executed" or \
            not isinstance(coordinates, list) or not coordinates or \
            any(not isinstance(item, dict) for item in coordinates):
        raise ValueError("prestate boundary pin requires observed-execution oracle evidence")
    blocks = {item.get("block_number") for item in coordinates}
    indexes = {item.get("transaction_index") for item in coordinates}
    hashes = {str(item.get("transaction_hash", "")).lower() for item in coordinates}
    if len(blocks) != 1 or len(indexes) != 1 or \
            hashes != {str(evidence.get("oracle_transaction_hash", "")).lower()}:
        raise ValueError("observed execution coordinates are not one pinned transaction")
    return verify_system_transaction_prestate(
        path, chain_id=chain_id, execution_block_number=blocks.pop(),
        execution_block_hash=evidence["block_hash"],
        observed_transaction_index=indexes.pop(),
        observed_transaction_hash=evidence["oracle_transaction_hash"])


def pin_candidate_at_prestate_boundary(candidates: list[dict[str, Any]],
                                       proof: dict[str, Any] | None,
                                       order_key: str) -> dict[str, Any]:
    """Return the single matching candidate repinned at the proved boundary."""
    if proof is None or proof.get("prestate_equivalent_to_block_boundary") is not True:
        raise ValueError("prestate boundary pin requires a verified prestate proof")
    key = str(order_key).lower()
    selected = [row for row in candidates if str(row.get("order_key", "")).lower() == key]
    if len(selected) != 1:
        raise ValueError("prestate boundary pin requires exactly one matching candidate")
    candidate = selected[0]
    creation, terminal = candidate.get("creation") or {}, candidate.get("terminal") or {}
    block, execution = proof["equivalent_pin_block"], proof["execution_block_number"]
    if terminal.get("block_number") != execution or \
            str(terminal.get("transaction_hash", "")).lower() != \
            proof["observed_transaction_hash"]:
        raise ValueError("prestate proof does not name this order's recorded execution")
    if not isinstance(creation.get("block_number"), int) or \
            not creation["block_number"] <= block < execution:
        raise ValueError("prestate boundary is outside the order's pending interval")
    candidate["proposed_pin_block"] = block
    candidate["proposed_pin_hash"] = proof["equivalent_pin_block_hash"]
    candidate["pin_source"] = PIN_SOURCE
    return candidate
