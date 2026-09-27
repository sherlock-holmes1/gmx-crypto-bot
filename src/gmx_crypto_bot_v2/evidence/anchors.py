"""Resolve recorded block identities for historical configuration."""

from __future__ import annotations

from pathlib import Path

from gmx_crypto_bot_v2.evidence.repository import event_rows


def _recorded_block_hash(recording: Path, block: int) -> str:
    for event in event_rows(recording):
        if (
            event.get("kind") == "opening_state_checkpoint"
            and event["payload"].get("block_number") == block
        ):
            return event["payload"]["block_hash"]
        if event.get("kind") == "block_header" and event.get("block_number") == block:
            payload = event["payload"]
            return (
                payload.get("hash")
                or payload.get("block_hash")
                or payload["header"]["hash"]
            )
    raise ValueError(f"missing recorded header for configuration anchor {block}")
