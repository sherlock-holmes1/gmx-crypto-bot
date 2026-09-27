"""Shared check outcome aggregation."""

from __future__ import annotations


def _comparison(passed: bool, mismatch: str, mismatches: list[str]) -> str:
    if passed:
        return "matched"
    mismatches.append(mismatch)
    return "mismatch"
