"""Block timestamps from the shared evidence repository."""

from gmx_crypto_bot_v2.evidence.repository import block_timestamps


def load_timestamps(recording, hashes):
    return block_timestamps(recording, hashes)
