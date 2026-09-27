"""Compatible validation report records and per-order serialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ValidationReport:
    schema: str
    version: int
    recording: str
    orders_created: int
    opening_terminal_orders: int
    terminal_orders: int
    matched: int
    mismatched: int
    unresolved: int
    decode_errors: int
    implemented_checks_pass: bool
    economic_calibration_complete: bool
    complete: bool
    mismatch_counts: dict[str, int]
    check_counts: dict[str, dict[str, int]]
    remaining_economic_checks: list[str]
    orders: list[dict[str, Any]]
    historical_configuration_complete: bool = False
    accrual_validation: dict[str, Any] | None = None
    execution_fee_validation: dict[str, Any] | None = None


def _order_result(
    key: str,
    request: dict[str, Any],
    final_request: dict[str, Any],
    terminal: dict[str, Any] | None,
    observed: list[dict[str, Any]],
    checks: dict[str, str],
    mismatches: list[str],
    status: str,
) -> dict[str, Any]:
    result = {
        "key": key,
        "origin": request.get("source", "created_in_window"),
        "status": status,
        "request": request,
        "final_request": final_request,
        "terminal": terminal,
        "observed_execution_events": observed,
        "checks": checks,
        "mismatches": mismatches,
    }
    if terminal is not None:
        if request.get("source") == "opening_checkpoint":
            result["blocks_since_opening_checkpoint"] = (
                terminal["block_number"] - request["block_number"]
            )
        else:
            result["request_to_terminal_blocks"] = (
                terminal["block_number"] - request["block_number"]
            )
    return result
