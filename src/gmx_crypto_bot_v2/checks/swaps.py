"""Compare per-hop swap models with recorded executions."""

from __future__ import annotations

from gmx_crypto_bot_v2.reconstruction.swaps import CHECKS, coordinate


def compare_swaps(events, results, checks, errors, request=None):
    count = sum(e["event_name"] == "SwapInfo" for e in events)
    if not count and not results:
        return
    expected = {coordinate(e) for e in events if e["event_name"] == "SwapInfo"}
    actual = {tuple(r["coordinate"]) for r in results}
    receipt_values = {
        coordinate(e): e["values"] for e in events if e["event_name"] == "SwapInfo"
    }
    conflicting = any(
        tuple(r["coordinate"]) in receipt_values
        and r["observed_swap"] != receipt_values[tuple(r["coordinate"])]
        for r in results
    )
    for name in CHECKS:
        outcomes = [r["checks"][name] for r in results]
        status = (
            "mismatch"
            if "mismatch" in outcomes
            else (
                "unavailable"
                if len(results) != count
                or actual != expected
                or "unavailable" in outcomes
                else "matched"
            )
        )
        if (
            name == "historical_swap_fees"
            and request is not None
            and any(
                r.get("ui_fee_receiver") is not None
                and r["ui_fee_receiver"] != request.get("uiFeeReceiver")
                for r in results
            )
        ):
            status = "mismatch"
        if name == "independent_swap_state" and conflicting:
            status = "mismatch"
        checks[name] = status
        if status == "mismatch":
            errors.append(name + "_mismatch")
