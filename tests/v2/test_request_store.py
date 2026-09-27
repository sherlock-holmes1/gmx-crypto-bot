import base64
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmx_crypto_bot_v2.sources.request_store import RequestStore, request_key


class RequestStoreTests(unittest.TestCase):
    def test_migration_restart_namespace_and_corruption(self):
        payload = {"id": 1, "method": "eth_call", "params": ["0x123", "0x10"]}
        body = b'{"id":1,"result":"0x123"}'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "rpc" / "base-public"
            directory.mkdir(parents=True)
            path = directory / (request_key(payload) + ".json.gz")
            path.write_bytes(
                gzip.compress(
                    json.dumps(
                        {
                            "request": payload,
                            "body": base64.b64encode(body).decode(),
                            "sha256": hashlib.sha256(body).hexdigest(),
                        }
                    ).encode()
                )
            )
            # Crash after commit but before unlink: restart must finish migration.
            with patch.object(Path, "unlink", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    RequestStore(directory)
            self.assertTrue(path.exists())
            store = RequestStore(directory)
            self.assertFalse(directory.exists())
            self.assertEqual(store.get(payload), (payload, body))
            self.assertEqual(
                RequestStore(directory).get(dict(payload, id=9)), (payload, body)
            )
            other = RequestStore(directory.parent / "base-archive")
            self.assertIsNone(other.get(payload))
            with store.connect() as connection:
                connection.execute("UPDATE responses SET body_sha256='bad'")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                store.get(payload)

    def test_invalid_legacy_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "rpc" / "base-public"
            directory.mkdir(parents=True)
            path = directory / "bad.json.gz"
            path.write_bytes(
                gzip.compress(
                    json.dumps(
                        {
                            "request": {"id": 1, "method": "eth_call", "params": []},
                            "body": "",
                            "sha256": "bad",
                        }
                    ).encode()
                )
            )
            with self.assertRaisesRegex(ValueError, "corrupt"):
                RequestStore(directory)
            self.assertTrue(path.exists())
