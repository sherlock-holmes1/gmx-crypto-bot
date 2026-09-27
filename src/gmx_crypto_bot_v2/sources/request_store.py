"""Durable compressed RPC responses, independent of the derived evidence catalog."""

import base64
import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def request_key(payload):
    items = payload if isinstance(payload, list) else [payload]
    canonical = [{"method": item["method"], "params": item["params"]} for item in items]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class RequestStore:
    def __init__(self, directory: Path):
        self.path = directory.parent / "requests.sqlite"
        self.namespace = directory.name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS responses (
                namespace TEXT NOT NULL, request_hash TEXT NOT NULL,
                request_json TEXT NOT NULL, compressed_body BLOB NOT NULL,
                body_sha256 TEXT NOT NULL,
                PRIMARY KEY(namespace, request_hash))""")
        self.migrate(directory)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=60)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def get(self, payload):
        key = request_key(payload)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT request_json, compressed_body, body_sha256 FROM responses WHERE namespace=? AND request_hash=?",
                (self.namespace, key),
            ).fetchone()
        if row is None:
            return None
        original, compressed, expected = row
        body = gzip.decompress(compressed)
        original = json.loads(original)
        if hashlib.sha256(body).hexdigest() != expected or request_key(original) != key:
            raise ValueError("corrupt durable RPC response")
        return original, body

    def put(self, payload, body):
        key = request_key(payload)
        existing = self.get(payload)
        if existing is not None:
            if existing != (payload, body):
                raise ValueError("conflicting durable RPC response")
            return
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO responses VALUES (?, ?, ?, ?, ?)",
                (
                    self.namespace,
                    key,
                    json.dumps(payload),
                    gzip.compress(body),
                    hashlib.sha256(body).hexdigest(),
                ),
            )

    def migrate(self, directory):
        """Commit and verify each legacy response before removing its source file."""
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json.gz")):
            saved = json.loads(gzip.decompress(path.read_bytes()))
            payload = saved["request"]
            body = base64.b64decode(saved["body"], validate=True)
            if (
                request_key(payload) + ".json.gz" != path.name
                or hashlib.sha256(body).hexdigest() != saved["sha256"]
            ):
                raise ValueError("corrupt legacy RPC work unit")
            self.put(payload, body)
            if self.get(payload) != (payload, body):
                raise ValueError("RPC migration verification failed")
            path.unlink()
        if not any(directory.iterdir()):
            directory.rmdir()
