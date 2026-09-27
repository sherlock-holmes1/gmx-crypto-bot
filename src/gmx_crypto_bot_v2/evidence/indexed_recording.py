"""Repeatable, bounded-memory canonical recording view."""

from gmx_crypto_bot_v2.evidence.recording import RecordedEvent
from gmx_crypto_bot_v2.evidence.repository import EvidenceRepository


class IndexedEvents:
    def __init__(self, repository):
        self.repository = repository

    def __len__(self):
        return self.repository.catalog.connection.execute(
            "SELECT count(*) FROM events"
        ).fetchone()[0]

    def __iter__(self):
        for row in self.repository.events(canonical=True):
            row.pop("_decoded", None)
            yield RecordedEvent(**row)

    def close(self):
        self.repository.close()


def load_indexed_recording(path, cache_directory=None):
    if path.is_file():
        path = path.parent
    repository = EvidenceRepository(path, cache_directory)
    return IndexedEvents(repository), repository.metadata
