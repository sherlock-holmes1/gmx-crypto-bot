"""Split provider-limited log ranges while preserving explicit gaps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator

from gmx_crypto_bot_v2.sources.rpc import SourceError


@dataclass(frozen=True)
class RangeGap:
    source: str
    from_block: int
    to_block: int
    error: str


def adaptive_ranges(
    fetch: Callable[[int, int], list[dict[str, Any]]],
    start: int,
    end: int,
    on_gap: Callable[[RangeGap], None],
    *,
    source: str,
) -> Iterator[tuple[int, int, list[dict[str, Any]]]]:
    """Split rejected log ranges until each readable range or explicit one-block gap."""
    try:
        yield start, end, fetch(start, end)
        return
    except SourceError as error:
        if start == end:
            on_gap(RangeGap(source, start, end, str(error)))
            return
    midpoint = start + (end - start) // 2
    yield from adaptive_ranges(fetch, start, midpoint, on_gap, source=source)
    yield from adaptive_ranges(fetch, midpoint + 1, end, on_gap, source=source)
