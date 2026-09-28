"""Durable transaction and gas-probe evidence, independent of derived indexes."""

import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class TraceStore:
    def __init__(self, recording: Path, *, read_only=False):
        self.recording = recording
        self.path = recording / ".collection/traces.sqlite"
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self.connect()) as connection, connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS evidence (
                    kind TEXT NOT NULL, transaction_hash TEXT NOT NULL,
                    compressed_body BLOB NOT NULL, body_sha256 TEXT NOT NULL,
                    PRIMARY KEY(kind, transaction_hash))""")

    def connect(self):
        if self.read_only:
            return sqlite3.connect(
                "file:" + str(self.path.resolve()) + "?mode=ro", uri=True
            )
        connection = sqlite3.connect(self.path, timeout=60)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def get_bytes(self, kind, transaction):
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT compressed_body,body_sha256 FROM evidence WHERE kind=? AND transaction_hash=?",
                (kind, transaction),
            ).fetchone()
        if row is None:
            return None
        body = gzip.decompress(row[0])
        if sha256(body) != row[1]:
            raise ValueError("corrupt stored trace evidence: " + transaction)
        return body

    def get(self, kind, transaction):
        body = self.get_bytes(kind, transaction)
        return None if body is None else json.loads(body)

    def put(self, kind, transaction, value):
        if value.get("transaction_hash") != transaction:
            raise ValueError("trace transaction identity mismatch")
        body = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        existing = self.get_bytes(kind, transaction)
        if existing is not None:
            if existing != body:
                raise ValueError("conflicting stored trace evidence: " + transaction)
            return sha256(body)
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO evidence VALUES (?,?,?,?)",
                (kind, transaction, gzip.compress(body, compresslevel=1), sha256(body)),
            )
        if self.get_bytes(kind, transaction) != body:
            raise ValueError("stored trace failed verification: " + transaction)
        return sha256(body)

    def migrate(self, journal):
        legacy = self.recording / "execution-fee-traces"
        if not legacy.exists():
            return 0
        count = 0
        for path in sorted(legacy.glob("*.json")):
            kind = "gas" if path.name.endswith(".gas-v2.json") else "trace"
            transaction = path.name.removesuffix(
                ".gas-v2.json" if kind == "gas" else ".json"
            )
            if (
                len(transaction) != 66
                or not transaction.startswith("0x")
                or any(c not in "0123456789abcdef" for c in transaction[2:])
            ):
                raise ValueError("invalid legacy trace filename")
            body = path.read_bytes()
            unit = kind + ":" + transaction
            entry = journal.state["units"].get(unit)
            if entry:
                expected = entry.get("artifacts", {}).get(
                    str(path.relative_to(self.recording))
                )
                if expected is not None and sha256(body) != expected:
                    raise ValueError(
                        "legacy trace differs from journal: " + transaction
                    )
            value = json.loads(body)
            digest = self.put(kind, transaction, value)
            journal.complete_store(unit, kind, transaction, digest)
            path.unlink()
            count += 1
        if not any(legacy.iterdir()):
            legacy.rmdir()
        return count
