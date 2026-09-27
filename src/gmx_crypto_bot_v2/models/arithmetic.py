"""Signed division and rounding rules shared by economic models."""

from __future__ import annotations


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _proportional_pending_impact(amount: int, delta_size: int, total_size: int) -> int:
    numerator = amount * delta_size
    return (
        -_ceil_div(-numerator, total_size) if numerator < 0 else numerator // total_size
    )


def _trunc_div(numerator: int, denominator: int) -> int:
    return (1 if numerator >= 0 else -1) * (abs(numerator) // denominator)
