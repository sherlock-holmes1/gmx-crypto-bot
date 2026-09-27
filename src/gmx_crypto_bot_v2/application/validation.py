"""Assemble verified evidence, execute order checks and publish compatible reports."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.checks.execution import apply_execution_fee_proof
from gmx_crypto_bot_v2.checks.orders import validate_order
from gmx_crypto_bot_v2.domain.constants import TERMINAL_EVENTS, UNMODELED_ECONOMICS
from gmx_crypto_bot_v2.domain.entries import _coordinate
from gmx_crypto_bot_v2.domain.evidence import OrderContext
from gmx_crypto_bot_v2.evidence.traces import TraceRepository
from gmx_crypto_bot_v2.reconstruction.accrual import AccrualReplay
from gmx_crypto_bot_v2.reconstruction.context import ValidationContext
from gmx_crypto_bot_v2.reconstruction.fees import build_fee_histories
from gmx_crypto_bot_v2.reconstruction.impact import load_factor_histories
from gmx_crypto_bot_v2.reconstruction.orders import (
    _load_execution_receipt_evidence,
    _load_order_update_topups,
    _load_replay_evidence,
    _load_terminal_lifecycle,
)
from gmx_crypto_bot_v2.reconstruction.positions import _attach_pre_virtual_inventory
from gmx_crypto_bot_v2.reconstruction.referral import ReferralState
from gmx_crypto_bot_v2.reconstruction.swaps import SwapReplay
from gmx_crypto_bot_v2.reporting.orders import ValidationReport


def _validate_orders(recording: Path) -> ValidationReport:
    """Join target order requests to raw terminal events and observed execution events."""
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    traces = TraceRepository(recording)
    target_market = metadata["market"]["market_token_address"].lower()
    index_token = metadata.get("tokens", {}).get("index", {}).get("address", "").lower()
    (
        created,
        opening,
        observed,
        receipts,
        opening_positions,
        position_events,
        decode_errors,
    ) = _load_replay_evidence(recording, target_market, index_token)
    virtual_id = (
        metadata.get("pinned_configuration_raw", {})
        .get("virtual_index_token_id", "")
        .lower()
    )
    configuration_events: list[dict[str, Any]] = []
    swap_replay = SwapReplay(recording, metadata)
    swap_state_events: list[dict[str, Any]] = []
    lifecycle, execution_fees, virtual_events, raw_decode_errors = (
        _load_terminal_lifecycle(
            recording,
            set(created) | set(opening),
            virtual_id,
            configuration_events,
            swap_replay,
            swap_state_events,
        )
    )
    swap_replay.replay(
        swap_state_events, configuration_events, set(created) | set(opening)
    )
    _attach_pre_virtual_inventory(position_events, virtual_events)
    update_topups = _load_order_update_topups(recording, lifecycle, index_token)
    swaps, payouts = _load_execution_receipt_evidence(recording, lifecycle)
    impact_factors = load_factor_histories(recording, metadata)
    ui_receivers = {
        entry["values"]["uiFeeReceiver"].lower()
        for entries in observed.values()
        for entry in entries
        if entry["event_name"] == "PositionFeesCollected"
        and isinstance(entry["values"].get("uiFeeReceiver"), str)
    }
    fee_histories = build_fee_histories(
        recording, metadata, configuration_events, ui_receivers
    )
    referral_state = ReferralState(recording, metadata, configuration_events)
    accrual_replay = AccrualReplay(recording, metadata)
    accrual_replay.run(configuration_events)
    decode_errors += raw_decode_errors
    results: list[dict[str, Any]] = []
    mismatch_counts: Counter[str] = Counter()

    for key, request in sorted(created.items(), key=lambda item: _coordinate(item[1])):
        order = validate_order(
            OrderContext(
                key=key,
                request=request,
                lifecycle=lifecycle.get(key, []),
                observed=observed.get(key, []),
            ),
            ValidationContext(
                receipts=receipts,
                execution_fees=execution_fees,
                update_topups=update_topups,
                opening_positions=opening_positions,
                position_events=position_events,
                swap_events=swaps.get(key, []),
                payout_events=payouts.get(key, []),
                metadata=metadata,
                impact_factors=impact_factors,
                fee_histories=fee_histories,
                referral_state=referral_state,
                modeled_swaps=swap_replay.results.get(key, []),
                accrual_replay=accrual_replay,
                traces=traces,
                payout_receipt_available=key in payouts,
            ),
        )
        results.append(order)
        mismatch_counts.update(order["mismatches"])
    opening_terminal_orders = 0
    for key, request in sorted(opening.items()):
        if key in created or not any(
            entry["event_name"] in TERMINAL_EVENTS for entry in lifecycle.get(key, [])
        ):
            continue
        order = validate_order(
            OrderContext(
                key=key,
                request=request,
                lifecycle=lifecycle[key],
                observed=observed.get(key, []),
            ),
            ValidationContext(
                receipts=receipts,
                execution_fees=execution_fees,
                update_topups=update_topups,
                opening_positions=opening_positions,
                position_events=position_events,
                swap_events=swaps.get(key, []),
                payout_events=payouts.get(key, []),
                metadata=metadata,
                impact_factors=impact_factors,
                fee_histories=fee_histories,
                referral_state=referral_state,
                modeled_swaps=swap_replay.results.get(key, []),
                accrual_replay=accrual_replay,
                traces=traces,
                payout_receipt_available=key in payouts,
            ),
        )
        results.append(order)
        mismatch_counts.update(order["mismatches"])
        opening_terminal_orders += 1

    execution_fee_summary = apply_execution_fee_proof(traces, results)
    mismatch_counts = Counter(
        error for order in results for error in order["mismatches"]
    )
    matched = sum(order["status"] == "matched" for order in results)
    mismatched = sum(order["status"] == "mismatch" for order in results)
    unresolved = sum(order["status"] == "unresolved" for order in results)
    check_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for order in results:
        for check, outcome in order["checks"].items():
            check_counts[check][outcome] += 1
    accrual_summary = accrual_replay.summary()
    for event, check in [
        ("Funding", "funding_update_derivation"),
        ("CumulativeBorrowingFactorUpdated", "borrowing_update_derivation"),
    ]:
        counts = accrual_summary["counts"][event]
        check_counts[check].update(counts or {"unavailable": 1})
    check_counts["accrual_closing_state"][
        "matched" if accrual_summary["complete"] else "unavailable"
    ] += 1
    if (
        any(r["status"] == "mismatch" for r in accrual_summary["updates"])
        or any(c["status"] == "mismatch" for c in accrual_summary["closing"].values())
        or accrual_summary["errors"]
        or accrual_summary["continuity_errors"]
    ):
        mismatch_counts["accrual_reconstruction_mismatch"] += 1
    remaining_checks = sorted(
        {
            check
            for check, counts in check_counts.items()
            if counts.get("unavailable", 0)
        }
        | set(UNMODELED_ECONOMICS)
        | (
            {"ambiguous_order_update_topups"}
            if check_counts["execution_fee_event_balance"].get(
                "unavailable_update_receipt", 0
            )
            else set()
        )
    )
    configuration_checks = (
        "historical_position_fee_factor",
        "historical_position_fee_amount",
        "historical_position_fee_receiver_factor",
        "historical_borrowing_fee_receiver_factor",
        "historical_ui_fee_factor",
        "historical_ui_fee_amount",
        "historical_referral_identity",
        "historical_referral_discount",
        "historical_pro_discount",
        "historical_protocol_fee",
        "independent_price_impact",
        "historical_swap_fees",
        "independent_swap_price_impact",
        "independent_swap_state",
        "independent_swap_output",
        "historical_fee_factors_liquidation",
        "independent_funding_accumulators",
        "independent_borrowing_accumulators",
    )
    configuration_complete = accrual_summary["complete"] and all(
        check_counts.get(name) and set(check_counts[name]) == {"matched"}
        for name in configuration_checks
    )
    if check_counts.get("liquidation_settlement") and set(
        check_counts["liquidation_settlement"]
    ) == {"matched"}:
        remaining_checks.remove("liquidation_settlement")
    if execution_fee_summary["complete"]:
        remaining_checks.remove("execution_fee_gas_and_transfer_proof")
    if configuration_complete:
        remaining_checks.remove("historical_configuration_factors")
    implemented_checks_pass = (
        not mismatched
        and not decode_errors
        and not mismatch_counts.get("accrual_reconstruction_mismatch")
    )
    economic_calibration_complete = not remaining_checks and implemented_checks_pass
    return ValidationReport(
        schema="GmxObservedOrderValidationReport",
        version=2,
        recording=str(recording),
        orders_created=len(created),
        opening_terminal_orders=opening_terminal_orders,
        terminal_orders=len(results) - unresolved,
        matched=matched,
        mismatched=mismatched,
        unresolved=unresolved,
        decode_errors=decode_errors,
        implemented_checks_pass=implemented_checks_pass,
        economic_calibration_complete=economic_calibration_complete,
        complete=economic_calibration_complete,
        mismatch_counts=dict(sorted(mismatch_counts.items())),
        check_counts={
            key: dict(sorted(counts.items()))
            for key, counts in sorted(check_counts.items())
        },
        remaining_economic_checks=remaining_checks,
        orders=results,
        historical_configuration_complete=configuration_complete,
        accrual_validation=accrual_summary,
        execution_fee_validation=execution_fee_summary,
    )


def validate_orders(
    recording: Path, *, cache_directory: Path | None = None
) -> ValidationReport:
    from gmx_crypto_bot_v2.evidence.repository import evidence_session

    with evidence_session(recording, cache_directory):
        return _validate_orders(recording)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate reconstructed GMX order outcomes against observed events."
    )
    parser.add_argument("recording", type=Path, help="GMX recording directory")
    parser.add_argument(
        "--output", type=Path, help="Write the complete JSON report to this file"
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the complete JSON report"
    )
    parser.add_argument(
        "--cache-dir", type=Path, help="Directory for the reusable SQLite catalog"
    )
    args = parser.parse_args()
    report = validate_orders(args.recording, cache_directory=args.cache_dir)
    result = asdict(report)
    if args.output:
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Orders created  {report.orders_created}")
        print(f"Opening closed  {report.opening_terminal_orders}")
        print(f"Terminal orders {report.terminal_orders}")
        print(f"Matched         {report.matched}")
        print(f"Mismatched      {report.mismatched}")
        print(f"Unresolved      {report.unresolved}")
        print(f"Decode errors   {report.decode_errors}")
        print(f"Checked fields {'PASS' if report.implemented_checks_pass else 'FAIL'}")
        print(
            f"Economics       {'COMPLETE' if report.economic_calibration_complete else 'PARTIAL'}"
        )
        print(f"Open checks     {', '.join(report.remaining_economic_checks)}")
        print(f"Validation      {'PASS' if report.complete else 'INCOMPLETE'}")
    return 0 if report.complete else 2
