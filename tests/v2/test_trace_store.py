import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmx_crypto_bot_v2.collection.journal import CollectionJournal
from gmx_crypto_bot_v2.evidence.trace_store import TraceStore
from gmx_crypto_bot_v2.evidence.traces import TraceRepository

TX = "0x" + "a" * 64


class TraceStoreTests(unittest.TestCase):
    def test_migrate_journaled_trace_and_resume_after_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "execution-fee-traces"
            legacy.mkdir()
            path = legacy / (TX + ".json")
            value = {
                "transaction_hash": TX,
                "block_number": 10,
                "receipt": {"status": "0x1"},
            }
            path.write_text(json.dumps(value))
            journal = CollectionJournal(root, {"test": "trace"})
            journal.complete("trace:" + TX, [path])
            store = TraceStore(root)
            with patch.object(Path, "unlink", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    store.migrate(journal)
            self.assertTrue(path.exists())
            self.assertTrue(journal.completed("trace:" + TX))
            self.assertEqual(store.migrate(journal), 1)
            self.assertFalse(legacy.exists())
            self.assertEqual(TraceRepository(root).get(TX).captured, value)
            self.assertTrue(
                CollectionJournal(root, {"test": "trace"}).completed("trace:" + TX)
            )
            with store.connect() as connection:
                connection.execute("UPDATE evidence SET body_sha256='bad'")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                TraceRepository(root).get(TX)

    def test_legacy_journal_mismatch_preserves_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "execution-fee-traces"
            legacy.mkdir()
            path = legacy / (TX + ".json")
            path.write_text(json.dumps({"transaction_hash": TX}))
            journal = CollectionJournal(root, {"test": "trace"})
            journal.complete("trace:" + TX, [path])
            path.write_text(json.dumps({"transaction_hash": TX, "changed": True}))
            with self.assertRaisesRegex(ValueError, "differs from journal"):
                TraceStore(root).migrate(journal)
            self.assertTrue(path.exists())
