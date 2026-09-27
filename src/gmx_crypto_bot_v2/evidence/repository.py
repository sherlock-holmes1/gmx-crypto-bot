"""Verified evidence access shared by collection, reconstruction and replay."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from gmx_crypto_bot_v2.evidence.catalog import EvidenceCatalog

_active = ContextVar("gmx_evidence_repository", default=None)


class EvidenceRepository:
    def __init__(self, directory: Path, cache_directory: Path | None = None):
        self.directory = directory
        self.catalog = EvidenceCatalog(directory, cache_directory)
        try:
            self.catalog.refresh()
            self.metadata = self.read_json("metadata.json")
        except BaseException:
            self.catalog.connection.close()
            raise

    def close(self):
        self.catalog.close()

    def read_json(self, name: str):
        return json.loads((self.directory / name).read_text())

    def exists(self, name: str):
        return (self.directory / name).exists()

    def events(self, **filters):
        return self.catalog.events(**filters)

    def logs(self, names=None):
        emitter = self.metadata["contracts"]["event_emitter"].lower()
        for log in self.catalog.logs(names):
            if log.get("address", "").lower() == emitter:
                yield log

    def receipts(self, wanted):
        for request, response in self.catalog.responses("eth_getTransactionReceipt"):
            params = request.get("params", [])
            if params and params[0].lower() in wanted:
                yield params[0].lower(), response.get("result") or {}

    def timestamps(self, hashes):
        timestamps = {}
        for request, response in self.catalog.responses("eth_getBlockByNumber"):
            params = request.get("params", [])
            if (
                not params
                or not isinstance(params[0], str)
                or not params[0].startswith("0x")
            ):
                continue
            block = int(params[0], 16)
            header = response.get("result")
            if block not in hashes or not isinstance(header, dict):
                continue
            if int(header["number"], 16) != block or header["hash"] != hashes[block]:
                raise ValueError("accrual header block hash mismatch")
            value = int(header["timestamp"], 16)
            if block in timestamps and timestamps[block] != value:
                raise ValueError("conflicting raw accrual timestamps")
            timestamps[block] = value
        return timestamps


@contextmanager
def evidence_session(directory: Path, cache_directory: Path | None = None):
    repository = EvidenceRepository(directory, cache_directory)
    token = _active.set(repository)
    try:
        yield repository
    finally:
        _active.reset(token)
        repository.close()


def active_repository(directory):
    repository = _active.get()
    if (
        repository is None
        or repository.directory.resolve() != Path(directory).resolve()
    ):
        raise RuntimeError("evidence reads require an evidence_session")
    return repository


def event_rows(directory, **filters):
    repository = _active.get()
    if (
        repository is not None
        and repository.directory.resolve() == Path(directory).resolve()
    ):
        yield from repository.events(**filters)
        return
    # Small standalone compatibility calls need no database or hidden writes.
    with (Path(directory) / "events.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if filters.get("kinds") and row["kind"] not in filters["kinds"]:
                continue
            if (
                filters.get("names")
                and row["payload"].get("event_name") not in filters["names"]
            ):
                continue
            yield row


def raw_logs(directory, names=None):
    repository = _active.get()
    if (
        repository is not None
        and repository.directory.resolve() == Path(directory).resolve()
    ):
        yield from repository.logs(names)
    else:
        with evidence_session(Path(directory)) as repository:
            yield from repository.logs(names)


def receipt_rows(directory, wanted):
    repository = _active.get()
    if (
        repository is not None
        and repository.directory.resolve() == Path(directory).resolve()
    ):
        yield from repository.receipts(wanted)
    else:
        with evidence_session(Path(directory)) as repository:
            yield from repository.receipts(wanted)


def block_timestamps(directory, hashes):
    repository = _active.get()
    if (
        repository is not None
        and repository.directory.resolve() == Path(directory).resolve()
    ):
        return repository.timestamps(hashes)
    with evidence_session(Path(directory)) as repository:
        return repository.timestamps(hashes)
