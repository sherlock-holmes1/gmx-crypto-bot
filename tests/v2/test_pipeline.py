"""V2 integration invariants: evidence provenance, discovery and durable resume."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmx_crypto_bot_v2.domain.events import DecodedEventLog
from gmx_crypto_bot_v2.evidence.artifacts import RawArtifactStore
from gmx_crypto_bot_v2.evidence.catalog import EvidenceCatalog
from gmx_crypto_bot_v2.evidence.discovery import discover
from gmx_crypto_bot_v2.evidence.publication import atomic_json, recording_writer
from gmx_crypto_bot_v2.evidence.recording import JsonlRecorder
from gmx_crypto_bot_v2.evidence.repository import evidence_session
from gmx_crypto_bot_v2.collection.journal import CollectionJournal
from gmx_crypto_bot_v2.collection.coordinator import CollectionCoordinator
from gmx_crypto_bot_v2.sources.resumable import ResumableRpc
from gmx_crypto_bot_v2.sources.rpc import PublicJsonRpc

MARKET = "0x" + "1" * 40
ACCOUNT = "0x" + "2" * 40
EMITTER = "0x" + "3" * 40
TX = "0x" + "4" * 64
ORDER = "0x" + "5" * 64
BLOCK_HASH = "0x" + "6" * 64


def make_recording(root):
    metadata = {
        "market": {"market_token_address": MARKET},
        "contracts": {"event_emitter": EMITTER},
        "spec_sha256": "fixture",
    }
    recorder = JsonlRecorder(root, metadata)
    recorder.record(
        "opening_state_checkpoint",
        {
            "block_number": 9,
            "block_hash": "opening",
            "orders": {
                ORDER: {
                    "opening_checkpoint": {
                        "account": ACCOUNT,
                        "orderType": 7,
                        "executionFee": 0,
                        "shouldUnwrapNativeToken": True,
                        "swapPath": [],
                    }
                }
            },
        },
        block_number=9,
        transaction_index=-1,
        log_index=-1,
    )
    recorder.record(
        "block_header",
        {"hash": BLOCK_HASH, "number": "0xa", "timestamp": "0x64"},
        block_number=10,
    )
    recorder.close()
    log = {
        "address": EMITTER,
        "blockNumber": "0xa",
        "transactionIndex": "0x0",
        "logIndex": "0x1",
        "transactionHash": TX,
        "blockHash": BLOCK_HASH,
        "data": "terminal",
    }
    artifacts = RawArtifactStore(root)
    artifacts.response(
        "rpc-eth_getLogs",
        {"method": "eth_getLogs", "params": [{"address": EMITTER}]},
        json.dumps({"result": [log]}).encode(),
    )
    artifacts.close()
    atomic_json(
        root / "completeness-report.json",
        {
            "complete": True,
            "gaps": [],
            "reorgs": [],
            "source_block_range": {"from": 10, "to": 10},
        },
    )
    return metadata


def decoder(_):
    return DecodedEventLog("OrderExecuted", {"key": ORDER, "largeValue": 2**255 + 17})


class CatalogTests(unittest.TestCase):
    def test_warm_reuse_rebuild_and_lossless_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            make_recording(root)
            with patch(
                "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                side_effect=decoder,
            ):
                with evidence_session(root) as repository:
                    baseline = list(repository.logs())
                    self.assertEqual(
                        repository.catalog.stats["bundles_decompressed"], 1
                    )
                    self.assertEqual(
                        baseline[0]["_decoded"]["values"]["largeValue"], 2**255 + 17
                    )
            with patch(
                "gmx_crypto_bot_v2.evidence.catalog.gzip.open",
                side_effect=AssertionError("warm decompression"),
            ):
                with evidence_session(root) as repository:
                    self.assertEqual(list(repository.logs()), baseline)
                    self.assertEqual(
                        repository.catalog.stats["bundles_decompressed"], 0
                    )
            (root / ".gmx-v2/catalog.sqlite").unlink()
            with patch(
                "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                side_effect=decoder,
            ):
                with evidence_session(root) as repository:
                    self.assertEqual(list(repository.logs()), baseline)

    def test_offline_validation_is_identical_after_catalog_deletion(self):
        from dataclasses import asdict
        from gmx_crypto_bot_v2.application.validation import validate_orders

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            metadata = make_recording(root)
            metadata["tokens"] = {
                name: {"address": ACCOUNT} for name in ("index", "long", "short")
            }
            atomic_json(root / "metadata.json", metadata)
            with patch(
                "socket.socket", side_effect=AssertionError("offline network access")
            ):
                with patch(
                    "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                    side_effect=decoder,
                ):
                    first = asdict(validate_orders(root))
                    with patch(
                        "gmx_crypto_bot_v2.evidence.catalog.gzip.open",
                        side_effect=AssertionError("warm decompression"),
                    ):
                        self.assertEqual(asdict(validate_orders(root)), first)
                    (root / ".gmx-v2/catalog.sqlite").unlink()
                    self.assertEqual(asdict(validate_orders(root)), first)

    def test_corrupt_database_is_reconstructed_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            make_recording(root)
            with patch(
                "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                side_effect=decoder,
            ):
                with evidence_session(root) as repository:
                    expected = list(repository.events())
                (root / ".gmx-v2/catalog.sqlite").write_bytes(b"corrupt sqlite")
                with patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("network forbidden"),
                ):
                    with evidence_session(root) as repository:
                        self.assertEqual(list(repository.events()), expected)

    def test_changed_manifest_and_missing_evidence_are_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            make_recording(root)
            with patch(
                "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                side_effect=decoder,
            ):
                with evidence_session(root):
                    pass
            manifest = root / "raw/manifest.jsonl"
            row = json.loads(manifest.read_text())
            row["sha256"] = "0" * 64
            manifest.write_text(json.dumps(row) + "\n")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                with evidence_session(root):
                    pass
            (root / "events.jsonl").unlink()
            with self.assertRaisesRegex(ValueError, "removed"):
                with evidence_session(root):
                    pass

    def test_discovery_includes_liquidation_trader_and_native_trace_without_report(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            make_recording(root)
            self.assertFalse((root / "order-validation.json").exists())
            with patch(
                "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                side_effect=decoder,
            ):
                with evidence_session(root):
                    result = discover(root)
            self.assertEqual(result.traders, frozenset({ACCOUNT}))
            self.assertEqual(result.traces[0].transaction_hash, TX)
            self.assertIn("native_payout", result.traces[0].reasons)


class ResumeTests(unittest.TestCase):
    def test_rpc_work_units_resume_logs_archive_batches_and_traces(self):
        class Sink:
            def response(self, *args):
                return "raw-ref"

            def error(self, *args):
                pass

        sent = []

        def send(client, source, request):
            sent.append(request)
            values = request if isinstance(request, list) else [request]
            responses = [{"id": item["id"], "result": "saved"} for item in values]
            response = responses if isinstance(request, list) else responses[0]
            client.artifacts.response(source, request, json.dumps(response).encode())
            return response

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(PublicJsonRpc, "_send", send):
                first = ResumableRpc("https://example.invalid", 1, Sink(), directory)
                self.assertEqual(
                    first.call("eth_getLogs", [{"fromBlock": "0x1", "toBlock": "0x2"}]),
                    "saved",
                )
                self.assertEqual(
                    first.call_many("eth_call", [[{"to": MARKET}, "0x1"]]), ["saved"]
                )
                self.assertEqual(
                    first.call("debug_traceTransaction", [TX, {}]), "saved"
                )
            with patch.object(
                PublicJsonRpc,
                "_send",
                side_effect=AssertionError("refetched completed work"),
            ):
                resumed = ResumableRpc("https://example.invalid", 1, Sink(), directory)
                resumed._request_id = 100
                resumed.call("eth_getLogs", [{"fromBlock": "0x1", "toBlock": "0x2"}])
                resumed.call_many("eth_call", [[{"to": MARKET}, "0x1"]])
                resumed.call("debug_traceTransaction", [TX, {}])
                self.assertEqual(resumed.reused, 3)
            self.assertEqual(len(sent), 3)

    def test_atomic_publication_failure_is_not_completed_and_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = CollectionJournal(root, {"market": MARKET})
            target = root / "snapshot.json"
            with patch(
                "gmx_crypto_bot_v2.evidence.publication.os.replace",
                side_effect=OSError("interrupted"),
            ):
                with self.assertRaises(OSError):
                    atomic_json(target, {"value": 1})
            self.assertFalse(target.exists())
            self.assertFalse(journal.completed("snapshot"))
            atomic_json(target, {"value": 1})
            journal.complete("snapshot", [target])
            resumed = CollectionJournal(root, {"market": MARKET})
            self.assertTrue(resumed.completed("snapshot"))
            target.write_text("{}")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                resumed.completed("snapshot")

    def test_recording_identity_and_single_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            CollectionJournal(root, {"market": MARKET})
            with self.assertRaisesRegex(ValueError, "identity"):
                CollectionJournal(root, {"market": "different"})
            with recording_writer(root):
                with self.assertRaisesRegex(ValueError, "another collector"):
                    with recording_writer(root):
                        pass

    def test_fresh_pipeline_and_repeated_run_need_no_validation_report(self):
        from gmx_crypto_bot_v2.collection.coordinator import SNAPSHOTS

        class Base:
            def __init__(self, spec, output, **kwargs):
                self.output = output

                class Closed:
                    def close(self):
                        pass

                self.artifacts = self.recorder = Closed()

            def collect(self, start, end):
                make_recording(self.output)
                return {"complete": True}

        def snapshot(stage):
            value = {"schema": SNAPSHOTS[stage][1], "version": 1, "market": MARKET}
            if stage in {"fees", "accrual"}:
                value["opening"] = {"block_number": 9, "block_hash": "opening"}
            elif stage == "impact":
                value.update(block_number=9, block_hash="opening")
            else:
                value.update(opening_block=9, opening_hash="opening")
            if stage == "referral":
                value["traders"] = [ACCOUNT]
            if stage == "swaps":
                value["markets"] = []
            return value

        trace = {
            "schema": "GmxExecutionFeeTrace",
            "version": 1,
            "transaction_hash": TX,
            "block_number": 10,
            "block_hash": BLOCK_HASH,
            "payments": [],
            "receipt": {
                "transactionHash": TX,
                "blockNumber": "0xa",
                "blockHash": BLOCK_HASH,
                "status": "0x1",
            },
            "transaction": {
                "hash": TX,
                "blockNumber": "0xa",
                "blockHash": BLOCK_HASH,
                "from": ACCOUNT,
                "to": EMITTER,
                "input": "0x",
            },
            "trace": {"from": ACCOUNT, "to": EMITTER, "input": "0x"},
        }
        from contextlib import ExitStack

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            spec = {
                "observation_window": {"end_block": 10},
                "anchor_block": {"rpc_url": "https://example.invalid"},
            }
            with ExitStack() as stack:
                stack.enter_context(
                    patch("gmx_crypto_bot_v2.collection.coordinator.GmxCollector", Base)
                )
                stack.enter_context(
                    patch(
                        "gmx_crypto_bot_v2.evidence.catalog.decode_event_log",
                        side_effect=decoder,
                    )
                )
                for stage, function in [
                    ("impact", "fetch_opening_impact_factors"),
                    ("fees", "fetch_opening_fee_configuration"),
                    ("accrual", "collect_accrual"),
                    ("referral", "collect_referral"),
                    ("swaps", "collect_swaps"),
                ]:
                    stack.enter_context(
                        patch(
                            "gmx_crypto_bot_v2.collection.coordinator." + function,
                            return_value=snapshot(stage),
                        )
                    )
                fetch = stack.enter_context(
                    patch(
                        "gmx_crypto_bot_v2.collection.coordinator.collect_transaction",
                        return_value=trace,
                    )
                )
                coordinator = CollectionCoordinator(
                    root,
                    spec=spec,
                    from_block=10,
                    to_block=10,
                    archive_rpc_url="https://example.invalid",
                    progress=lambda *_: None,
                )
                self.assertTrue(coordinator.run()["ready"])
                self.assertEqual(fetch.call_count, 1)
                self.assertFalse((root / "order-validation.json").exists())
                with patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("network on complete resume"),
                ):
                    resumed = CollectionCoordinator(
                        root, resume=True, progress=lambda *_: None
                    )
                    self.assertTrue(resumed.run()["ready"])
                self.assertEqual(fetch.call_count, 1)


class PublicationResumeTests(unittest.TestCase):
    def test_interrupted_base_publication_reuses_completed_capture(self):
        from gmx_crypto_bot_v2.evidence.catalog import digest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            root.mkdir()
            scratch = root / ".collection/base-test"
            make_recording(scratch)
            identity = {"spec_sha256": None, "from_block": 10, "to_block": 10}
            journal = CollectionJournal(root, identity)
            atomic_json(
                root / ".collection/base-ready.json",
                {
                    "directory": ".collection/base-test",
                    "artifacts": {
                        str(p.relative_to(scratch)): digest(p)
                        for p in scratch.rglob("*")
                        if p.is_file()
                    },
                },
            )
            collector = CollectionCoordinator(
                root, resume=True, progress=lambda *_: None
            )
            collector.journal = journal
            with patch(
                "gmx_crypto_bot_v2.collection.coordinator.atomic_copy",
                side_effect=OSError("interrupted publication"),
            ):
                with self.assertRaises(OSError):
                    collector._base()
            self.assertFalse(journal.completed("base"))
            with patch(
                "gmx_crypto_bot_v2.collection.coordinator.GmxCollector",
                side_effect=AssertionError("refetched base"),
            ):
                collector._base()
            self.assertTrue(journal.completed("base"))
            self.assertEqual(
                (root / "events.jsonl").read_bytes(),
                (scratch / "events.jsonl").read_bytes(),
            )

    def test_snapshot_published_before_journal_is_adopted_on_resume(self):
        from gmx_crypto_bot_v2.domain.evidence import CollectionRequirements

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            make_recording(root)
            collector = CollectionCoordinator(
                root, resume=True, progress=lambda *_: None
            )
            collector.journal = CollectionJournal(root, {"test": "snapshot"})
            requirements = CollectionRequirements(
                frozenset(), frozenset(), frozenset(), frozenset(), ()
            )
            snapshot = {
                "schema": "GmxImpactOpeningConfiguration",
                "version": 1,
                "market": MARKET,
                "block_number": 9,
                "block_hash": "opening",
            }

            class Archive:
                def call(self, *args):
                    raise AssertionError("unexpected RPC")

            with patch(
                "gmx_crypto_bot_v2.collection.coordinator.fetch_opening_impact_factors",
                return_value=snapshot,
            ):
                with patch.object(
                    collector.journal,
                    "complete",
                    side_effect=OSError("interrupted journal publication"),
                ):
                    with self.assertRaises(OSError):
                        collector._snapshot("impact", Archive(), None, requirements)
            collector.journal = CollectionJournal(root, {"test": "snapshot"})
            self.assertTrue(collector._snapshot("impact", None, None, requirements))
            self.assertTrue(collector.journal.completed("impact"))

    def test_trace_published_before_journal_is_reverified_on_resume(self):
        from gmx_crypto_bot_v2.domain.evidence import (
            CollectionRequirements,
            TraceRequirement,
            BlockReference,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "recording"
            make_recording(root)
            collector = CollectionCoordinator(
                root, resume=True, progress=lambda *_: None
            )
            collector.journal = CollectionJournal(root, {"test": "trace"})
            requirements = CollectionRequirements(
                frozenset(),
                frozenset(),
                frozenset(),
                frozenset(),
                (
                    TraceRequirement(
                        TX, BlockReference(10, BLOCK_HASH), frozenset({"native_payout"})
                    ),
                ),
            )
            trace = {
                "schema": "GmxExecutionFeeTrace",
                "version": 1,
                "transaction_hash": TX,
                "block_number": 10,
                "block_hash": BLOCK_HASH,
                "payments": [],
                "receipt": {
                    "transactionHash": TX,
                    "blockNumber": "0xa",
                    "blockHash": BLOCK_HASH,
                    "status": "0x1",
                },
                "transaction": {
                    "hash": TX,
                    "blockNumber": "0xa",
                    "blockHash": BLOCK_HASH,
                    "from": ACCOUNT,
                    "to": EMITTER,
                    "input": "0x",
                },
                "trace": {"from": ACCOUNT, "to": EMITTER, "input": "0x"},
            }
            with patch(
                "gmx_crypto_bot_v2.collection.coordinator.collect_transaction",
                return_value=trace,
            ):
                with patch.object(
                    collector.journal,
                    "complete",
                    side_effect=OSError("interrupted journal publication"),
                ):
                    with self.assertRaises(OSError):
                        collector._traces(object(), requirements)
            collector.journal = CollectionJournal(root, {"test": "trace"})
            self.assertEqual(collector._traces(None, requirements), [])
            self.assertTrue(collector.journal.completed("trace:" + TX))
            trace["receipt"]["blockHash"] = "changed"
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                collector._verify_trace_identity(trace, requirements.traces[0])


class BoundaryTests(unittest.TestCase):
    def test_layered_modules_have_no_import_cycles_or_check_file_access(self):
        root = Path(__file__).parents[2] / "src/gmx_crypto_bot_v2"
        layers = {
            "application",
            "checks",
            "collection",
            "domain",
            "evidence",
            "models",
            "reconstruction",
            "reporting",
            "sources",
        }
        graph = {}
        for directory in layers:
            for path in (root / directory).glob("*.py"):
                name = "gmx_crypto_bot_v2." + directory + "." + path.stem
                tree = ast.parse(path.read_text())
                graph[name] = {
                    node.module
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and (node.module or "").startswith("gmx_crypto_bot_v2.")
                }
                if directory == "checks":
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call) and isinstance(
                            node.func, ast.Attribute
                        ):
                            self.assertNotIn(
                                node.func.attr,
                                {"read_text", "read_bytes", "open", "exists"},
                                str(path),
                            )
        visited = set()

        def visit(name, stack):
            self.assertNotIn(
                name, stack, "import cycle: " + " -> ".join(stack + [name])
            )
            if name in visited:
                return
            for dependency in graph.get(name, ()):
                visit(dependency, stack + [name])
            visited.add(name)

        for name in graph:
            visit(name, [])

    def test_models_are_pure_and_collection_never_imports_validation(self):
        root = Path(__file__).parents[2] / "src/gmx_crypto_bot_v2"
        for directory in ("models", "domain"):
            for path in (root / directory).glob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(
                            any(
                                n.name.split(".")[0]
                                in {"sqlite3", "pathlib", "urllib", "argparse", "os"}
                                for n in node.names
                            ),
                            str(path),
                        )
                    if isinstance(node, ast.ImportFrom) and (
                        node.module or ""
                    ).startswith("gmx_crypto_bot_v2."):
                        self.assertIn(
                            node.module.split(".")[1], {"domain", "models"}, str(path)
                        )
        for path in (root / "collection").glob("*.py"):
            text = path.read_text()
            self.assertNotIn("order-validation.json", text, str(path))
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("validation", node.module or "", str(path))
        for path in root.rglob("*.py"):
            self.assertNotIn("from gmx_crypto_bot.", path.read_text(), str(path))


if __name__ == "__main__":
    unittest.main()
