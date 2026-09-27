"""Select additive V2 supplements without rewriting original recordings."""

from pathlib import Path


def snapshot_path(recording: Path, filename: str) -> Path:
    supplement = recording / ("v2-" + filename)
    return supplement if supplement.exists() else recording / filename
