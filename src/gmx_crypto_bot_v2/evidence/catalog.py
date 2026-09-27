"""Rebuildable SQLite projection of authoritative recording evidence.

Source fingerprints avoid reopening unchanged bundles. A changed source is verified
and replaced in a transaction; interrupted indexing never publishes partial rows.
Financial values stay in JSON (Python integers), never SQLite REAL columns.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

from gmx_crypto_bot_v2.domain.events import (
    EventDecodeError,
    decode_event_log,
    event_name_from_data,
)

SCHEMA_VERSION = 1
DECODER_VERSION = 1


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class EvidenceCatalog:
    def __init__(self, recording: Path, cache_directory: Path | None = None):
        self.recording = recording.resolve()
        directory = cache_directory or self.recording / ".gmx-v2"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "catalog.sqlite"
        self.stats = {
            "sources_indexed": 0,
            "sources_reused": 0,
            "bundles_decompressed": 0,
        }
        self.integrity_stamp = self.path.with_suffix(".verified.json")
        self.connection = self._connect()

    def _connect(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=60)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            try:
                stamp = json.loads(self.integrity_stamp.read_text())
            except (OSError, ValueError):
                stamp = None
            unchanged = stamp == fingerprint(self.path)
            valid = (
                unchanged
                or connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            )
            if not valid or version not in (0, SCHEMA_VERSION):
                raise sqlite3.DatabaseError("incompatible cache")
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.close()
            self.path.unlink(missing_ok=True)
            connection = sqlite3.connect(self.path, timeout=60)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS identity (value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
                path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, kind TEXT, block INTEGER, tx_index INTEGER,
                log_index INTEGER, event_name TEXT, order_key TEXT, tx_hash TEXT,
                market TEXT, account TEXT, payload TEXT NOT NULL, decoded TEXT);
            CREATE INDEX IF NOT EXISTS events_coordinate ON events(block,tx_index,log_index,seq);
            CREATE INDEX IF NOT EXISTS events_replay_order ON events(CASE WHEN block IS NULL OR tx_index IS NULL OR log_index IS NULL THEN 1 ELSE 0 END,CASE WHEN block IS NOT NULL AND tx_index IS NOT NULL AND log_index IS NOT NULL THEN block ELSE 0 END,CASE WHEN block IS NOT NULL AND tx_index IS NOT NULL AND log_index IS NOT NULL THEN tx_index ELSE 0 END,CASE WHEN block IS NOT NULL AND tx_index IS NOT NULL AND log_index IS NOT NULL THEN log_index ELSE 0 END,seq);
            CREATE INDEX IF NOT EXISTS events_kind ON events(kind,event_name);
            CREATE INDEX IF NOT EXISTS events_order ON events(order_key);
            CREATE INDEX IF NOT EXISTS events_tx ON events(tx_hash);
            CREATE INDEX IF NOT EXISTS events_market ON events(market,account);
            CREATE TABLE IF NOT EXISTS responses (
                source TEXT, ordinal INTEGER, method TEXT, request TEXT, payload TEXT,
                PRIMARY KEY(source,ordinal));
            CREATE INDEX IF NOT EXISTS responses_method ON responses(method);
            CREATE TABLE IF NOT EXISTS logs (
                source TEXT, ordinal INTEGER, item INTEGER, block INTEGER,
                tx_index INTEGER, log_index INTEGER, event_name TEXT, order_key TEXT,
                tx_hash TEXT, market TEXT, account TEXT, payload TEXT, decoded TEXT,
                PRIMARY KEY(source,ordinal,item));
            CREATE INDEX IF NOT EXISTS logs_coordinate ON logs(block,tx_index,log_index);
            CREATE INDEX IF NOT EXISTS logs_event ON logs(event_name);
            CREATE INDEX IF NOT EXISTS logs_order ON logs(order_key);
            CREATE INDEX IF NOT EXISTS logs_tx ON logs(tx_hash);
            CREATE INDEX IF NOT EXISTS logs_market ON logs(market,account);
        """)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        identity = encode(
            {
                "recording": str(self.recording),
                "metadata": digest(self.recording / "metadata.json"),
                "decoder": DECODER_VERSION,
            }
        )
        previous = connection.execute("SELECT value FROM identity").fetchone()
        if previous and previous[0] != identity:
            with connection:
                for table in ("events", "responses", "logs", "sources", "identity"):
                    connection.execute(f"DELETE FROM {table}")
        if not previous or previous[0] != identity:
            connection.execute("INSERT INTO identity VALUES (?)", (identity,))
        connection.commit()
        return connection

    def close(self):
        self.connection.close()
        from gmx_crypto_bot_v2.evidence.publication import atomic_json

        atomic_json(self.integrity_stamp, fingerprint(self.path))

    def _changed(self, path: Path):
        name = str(path.relative_to(self.recording))
        previous = self.connection.execute(
            "SELECT fingerprint FROM sources WHERE path=?", (name,)
        ).fetchone()
        if previous and previous[0] == fingerprint(path):
            self.stats["sources_reused"] += 1
            return False
        return True

    def _published(self, path: Path):
        self.connection.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?)",
            (str(path.relative_to(self.recording)), fingerprint(path), digest(path)),
        )
        self.stats["sources_indexed"] += 1

    @staticmethod
    def _decode(log):
        try:
            decoded = decode_event_log(log["data"])
            return decoded.event_name, decoded.values
        except (EventDecodeError, KeyError, TypeError, ValueError):
            return event_name_from_data(log.get("data", "")), None

    def refresh(self):
        events = self.recording / "events.jsonl"
        if (
            not events.exists()
            and self.connection.execute(
                "SELECT 1 FROM sources WHERE path='events.jsonl'"
            ).fetchone()
        ):
            raise ValueError("published normalized evidence removed")
        if events.exists() and self._changed(events):
            with self.connection:
                self.connection.execute("DELETE FROM events")
                with events.open() as stream:
                    for expected, line in enumerate(stream, 1):
                        row = json.loads(line)
                        if row["seq"] != expected:
                            raise ValueError("non-contiguous recording sequence")
                        log = row.get("payload", {}).get("log", {})
                        name, decoded = self._decode(log) if log else (None, None)
                        values = decoded or {}
                        self.connection.execute(
                            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                row["seq"],
                                row["kind"],
                                row.get("block_number"),
                                row.get("transaction_index"),
                                row.get("log_index"),
                                name,
                                values.get("key", values.get("orderKey")),
                                log.get("transactionHash"),
                                values.get("market"),
                                values.get("account"),
                                encode(row),
                                encode(decoded) if decoded is not None else None,
                            ),
                        )
                self._published(events)
        manifest = self.recording / "raw/manifest.jsonl"
        if not manifest.exists():
            if self.connection.execute("SELECT 1 FROM responses LIMIT 1").fetchone():
                raise ValueError("published raw manifest removed")
            return
        # Only the base raw stream is projected. Sidecars and traces are consumed
        # through the repository; their exact raw captures remain authoritative.
        manifest_known = self.connection.execute(
            "SELECT 1 FROM sources WHERE path='raw/manifest.jsonl'"
        ).fetchone()
        manifest_changed = self._changed(manifest)
        manifests = {}
        with manifest.open() as stream:
            for line in stream:
                item = json.loads(line)
                if item.get("kind") == "response":
                    entries = manifests.setdefault(item["bundle"], {})
                    if item["seq"] in entries:
                        raise ValueError("duplicate raw manifest identity")
                    entries[item["seq"]] = item
        known = {
            r[0]
            for r in self.connection.execute("SELECT DISTINCT source FROM responses")
        }
        if known - manifests.keys():
            raise ValueError("published raw bundle removed from manifest")
        for name, entries in sorted(manifests.items()):
            path = self.recording / name
            if not path.is_relative_to(self.recording) or ".." in Path(name).parts:
                raise ValueError("artifact path escapes recording")
            if not self._changed(path) and not (manifest_known and manifest_changed):
                continue
            with self.connection:
                self.connection.execute("DELETE FROM responses WHERE source=?", (name,))
                self.connection.execute("DELETE FROM logs WHERE source=?", (name,))
                seen = set()
                with gzip.open(path, "rt") as stream:
                    self.stats["bundles_decompressed"] += 1
                    for line in stream:
                        record = json.loads(line)
                        ordinal = record["seq"]
                        if ordinal not in entries:
                            raise ValueError("unpublished response inside raw bundle")
                        if ordinal in seen:
                            raise ValueError("duplicate raw response")
                        seen.add(ordinal)
                        entry = entries[ordinal]
                        body = base64.b64decode(record["body_base64"], validate=True)
                        sha = hashlib.sha256(body).hexdigest()
                        if (
                            sha != entry["sha256"]
                            or sha != record["body_sha256"]
                            or len(body) != entry["bytes"]
                        ):
                            raise ValueError("raw response digest mismatch")
                        request = entry.get("request", {})
                        method = (
                            request.get("method", "")
                            if isinstance(request, dict)
                            else ""
                        )
                        response = json.loads(body)
                        # Keep only evidence needed offline; HTTP observations live in events.
                        if method in {
                            "eth_getLogs",
                            "eth_getBlockByNumber",
                            "eth_getTransactionReceipt",
                        }:
                            self.connection.execute(
                                "INSERT INTO responses VALUES (?,?,?,?,?)",
                                (
                                    name,
                                    ordinal,
                                    method,
                                    encode(request),
                                    (
                                        encode(response)
                                        if method != "eth_getLogs"
                                        else "{}"
                                    ),
                                ),
                            )
                        if (
                            method == "eth_getLogs"
                            and isinstance(response, dict)
                            and isinstance(response.get("result"), list)
                        ):
                            for index, log in enumerate(response["result"]):
                                event_name, decoded = self._decode(log)
                                values = decoded or {}
                                self.connection.execute(
                                    "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (
                                        name,
                                        ordinal,
                                        index,
                                        int(log["blockNumber"], 16),
                                        int(log["transactionIndex"], 16),
                                        int(log["logIndex"], 16),
                                        event_name,
                                        values.get("key", values.get("orderKey")),
                                        log.get("transactionHash"),
                                        values.get("market"),
                                        values.get("account"),
                                        encode(log),
                                        (
                                            encode(decoded)
                                            if decoded is not None
                                            else None
                                        ),
                                    ),
                                )
                if seen != entries.keys():
                    raise ValueError("manifest references missing response")
                self._published(path)

        if manifest_changed:
            with self.connection:
                self._published(manifest)

    def events(self, *, kinds=None, names=None, canonical=False):
        clauses, parameters = [], []
        for column, values in (("kind", kinds), ("event_name", names)):
            if values:
                clauses.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                parameters.extend(values)
        query = "SELECT payload,event_name,decoded FROM events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY " + (
            "CASE WHEN block IS NULL OR tx_index IS NULL OR log_index IS NULL THEN 1 ELSE 0 END,CASE WHEN block IS NOT NULL AND tx_index IS NOT NULL AND log_index IS NOT NULL THEN block ELSE 0 END,CASE WHEN block IS NOT NULL AND tx_index IS NOT NULL AND log_index IS NOT NULL THEN tx_index ELSE 0 END,CASE WHEN block IS NOT NULL AND tx_index IS NOT NULL AND log_index IS NOT NULL THEN log_index ELSE 0 END,seq"
            if canonical
            else "seq"
        )
        for payload, name, decoded in self.connection.execute(query, parameters):
            value = json.loads(payload)
            if decoded is not None:
                value["_decoded"] = {"event_name": name, "values": json.loads(decoded)}
            yield value

    def logs(self, names=None):
        # Preserve acquisition order without sorting multi-gigabyte payloads.
        # The event-name index remains useful for targeted coordinate lookups,
        # but SQLite otherwise chooses it and materializes a huge temporary sort.
        query = "SELECT payload,event_name,decoded FROM logs INDEXED BY sqlite_autoindex_logs_1"
        parameters = list(names or ())
        if names:
            query += " WHERE event_name IN (" + ",".join("?" for _ in names) + ")"
        query += " ORDER BY source,ordinal,item"
        for payload, name, decoded in self.connection.execute(query, parameters):
            log = json.loads(payload)
            if decoded is not None:
                log["_decoded"] = {"event_name": name, "values": json.loads(decoded)}
            yield log

    def responses(self, method):
        for request, payload in self.connection.execute(
            "SELECT request,payload FROM responses WHERE method=? ORDER BY source,ordinal",
            (method,),
        ):
            yield json.loads(request), json.loads(payload)
