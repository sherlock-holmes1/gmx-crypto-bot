"""One resumable evidence pipeline; readiness is distinct from economic validity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from gmx_crypto_bot_v2.collection.accrual import collect as collect_accrual
from gmx_crypto_bot_v2.collection.base import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONFIRMATIONS,
    GmxCollector,
)
from gmx_crypto_bot_v2.collection.fees import fetch_opening_fee_configuration
from gmx_crypto_bot_v2.collection.impact import fetch_opening_impact_factors
from gmx_crypto_bot_v2.collection.journal import CollectionJournal
from gmx_crypto_bot_v2.collection.referral import collect as collect_referral
from gmx_crypto_bot_v2.collection.swaps import collect as collect_swaps
from gmx_crypto_bot_v2.collection.traces import collect_gas_probe, collect_transaction
from gmx_crypto_bot_v2.collection.window import resolve_window
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.artifacts import RawArtifactStore
from gmx_crypto_bot_v2.evidence.catalog import digest
from gmx_crypto_bot_v2.evidence.discovery import discover
from gmx_crypto_bot_v2.evidence.publication import (
    atomic_copy,
    atomic_json,
    recording_writer,
)
from gmx_crypto_bot_v2.evidence.repository import evidence_session
from gmx_crypto_bot_v2.evidence.snapshots import snapshot_path
from gmx_crypto_bot_v2.evidence.trace_store import TraceStore
from gmx_crypto_bot_v2.sources.request_store import RequestStore
from gmx_crypto_bot_v2.sources.resumable import ResumableRpc

SNAPSHOTS = {
    "impact": ("impact-opening-configuration.json", "GmxImpactOpeningConfiguration"),
    "fees": ("fee-opening-configuration.json", "GmxFeeOpeningConfiguration"),
    "accrual": ("accrual-configuration.json", "GmxAccrualConfiguration"),
    "referral": ("liquidation-referral-configuration.json", "GmxReferralConfiguration"),
    "swaps": ("swap-opening-state.json", "GmxSwapOpeningState"),
}


class CollectionCoordinator:
    def __init__(
        self,
        output: Path,
        *,
        spec: dict | None = None,
        rpc_url: str | None = None,
        archive_rpc_url: str | None = None,
        resume=False,
        last_days=None,
        from_block=None,
        to_block=None,
        confirmations=DEFAULT_CONFIRMATIONS,
        chunk_size=DEFAULT_CHUNK_SIZE,
        timeout_seconds=90,
        gas_probes=False,
        cache_directory=None,
        progress=print,
    ):
        self.last_days = last_days
        self.output, self.spec, self.resume = output, spec, resume
        self.rpc_url = (
            rpc_url
            or (spec or {}).get("anchor_block", {}).get("rpc_url")
            or "https://arb1.arbitrum.io/rpc"
        )
        self.archive_url = archive_rpc_url
        self.start, self.end = from_block, to_block
        self.confirmations, self.chunk_size, self.timeout = (
            confirmations,
            chunk_size,
            timeout_seconds,
        )
        self.gas_probes, self.cache_directory, self.progress = (
            gas_probes,
            cache_directory,
            progress,
        )

    def run(self, stages=None):
        existed = self.output.exists()
        if existed and not self.resume:
            raise ValueError("recording exists; use --resume to verify and continue it")
        with recording_writer(self.output):
            # Migrate all roles, including stages already completed before resume.
            rpc_directory = self.output / ".collection/rpc"
            if rpc_directory.exists():
                for directory in sorted(rpc_directory.iterdir()):
                    if directory.is_dir():
                        RequestStore(directory)
            selection_files = self._select_window()
            identity = self._identity()
            self.journal = CollectionJournal(self.output, identity)
            trace_directory = self.output / "execution-fee-traces"
            if trace_directory.exists():
                migrated = TraceStore(self.output).migrate(self.journal)
                if migrated:
                    self.progress(
                        f"Migrated {migrated} trace evidence files into SQLite"
                    )
            verified_window = self.journal.completed("window")
            if selection_files and not verified_window:
                self.journal.complete("window", selection_files)
            atomic_json(
                self.output / "evidence-readiness.json",
                {
                    "schema": "GmxEvidenceReadinessReport",
                    "version": 1,
                    "ready": False,
                    "missing": ["collection_in_progress"],
                    "economic_validation": "not_run",
                },
            )
            self._base()
            with evidence_session(self.output, self.cache_directory) as repository:
                quality = repository.read_json("completeness-report.json")
                if (
                    not quality.get("complete")
                    or quality.get("gaps")
                    or quality.get("reorgs")
                ):
                    raise ValueError(
                        "base recording has gaps or reorgs; dependent collection stopped"
                    )
                requirements = discover(self.output)
                self.progress(
                    f"Discovery: {len(requirements.order_keys)} orders, {len(requirements.traders)} traders, {len(requirements.traces)} trace transactions"
                )
                capture = self.output / ".collection" / ("capture-" + uuid4().hex)
                capture.mkdir()
                artifacts = RawArtifactStore(capture)
                archive = (
                    self._rpc(self.archive_url, artifacts, "archive")
                    if self.archive_url
                    else None
                )
                logs = self._rpc(self.rpc_url, artifacts, "logs")
                try:
                    selected = set(stages or (*SNAPSHOTS, "traces"))
                    missing = []
                    for stage in SNAPSHOTS:
                        if stage in selected:
                            if not self._snapshot(stage, archive, logs, requirements):
                                missing.append("snapshot:" + stage)
                    missing += (
                        self._traces(archive, requirements)
                        if "traces" in selected
                        else ["trace stage not requested"]
                    )
                finally:
                    artifacts.close()
                coverage = {
                    "schema": "GmxEvidenceReadinessReport",
                    "version": 1,
                    "ready": not missing and selected == set((*SNAPSHOTS, "traces")),
                    "missing": missing,
                    "required_traders": len(requirements.traders),
                    "required_transactions": len(requirements.traces),
                    "collection_identity": self.journal.state["identity"],
                    "catalog": repository.catalog.stats,
                    "economic_validation": "not_run",
                }
                atomic_json(self.output / "evidence-readiness.json", coverage)
                return coverage

    def _select_window(self):
        selection_path = self.output / ".collection/window-selection.json"

        def spec_digest(spec):
            return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

        if selection_path.exists():
            selection = json.loads(selection_path.read_text())
            if self.last_days is not None and self.last_days != selection["days"]:
                raise ValueError("requested days differ from saved window")
            if self.spec and spec_digest(self.spec) not in {
                selection["template_sha256"],
                spec_digest(selection["spec"]),
            }:
                raise ValueError("spec differs from saved window template")
            if self.start is not None or self.end is not None:
                raise ValueError("saved rolling window cannot override block bounds")
            for name, expected in selection["artifacts"].items():
                if digest(self.output / name) != expected:
                    raise ValueError("window source evidence changed")
        elif self.last_days is not None:
            if (self.output / ".collection/journal.json").exists() or (
                self.output / "metadata.json"
            ).exists():
                raise ValueError(
                    "cannot change an existing recording to a rolling window"
                )
            if self.start is not None or self.end is not None:
                raise ValueError("rolling window cannot override block bounds")
            if not self.spec or not self.archive_url:
                raise ValueError("rolling window requires a spec and archive provider")
            capture = self.output / ".collection" / ("window-" + uuid4().hex)
            capture.mkdir(parents=True)
            artifacts = RawArtifactStore(capture)
            try:
                rpc = self._rpc(self.archive_url, artifacts, "window")
                selected = resolve_window(
                    self.spec, rpc.call, self.last_days, self.confirmations
                )
            finally:
                artifacts.close()
            selection = {
                "days": self.last_days,
                "template_sha256": spec_digest(self.spec),
                "spec": selected,
                "artifacts": {
                    str(p.relative_to(self.output)): digest(p)
                    for p in capture.rglob("*")
                    if p.is_file()
                },
            }
            atomic_json(selection_path, selection)
        else:
            return []
        self.spec = selection["spec"]
        window = self.spec["observation_window"]
        self.progress(
            f"Selected window: {window['start_utc']} through {window['end_inclusive_utc']} (blocks {window['start_block']}–{window['end_block']}); fixed on resume"
        )
        return [
            selection_path,
            *(self.output / name for name in selection["artifacts"]),
        ]

    def _identity(self):
        metadata = self.output / "metadata.json"
        identity = {
            "spec_sha256": hashlib.sha256(
                json.dumps(self.spec, sort_keys=True).encode()
            ).hexdigest()
            if self.spec
            else None,
            "from_block": self.start,
            "to_block": self.end,
        }
        journal = self.output / ".collection/journal.json"
        if journal.exists():
            previous = json.loads(journal.read_text())["identity"]
            for name, value in identity.items():
                if value is not None and previous.get(name) != value:
                    raise ValueError(
                        "requested collection identity differs from journal"
                    )
            return previous
        if metadata.exists():
            recorded = json.loads(metadata.read_text())
            if self.spec and recorded.get("spec_sha256") != identity["spec_sha256"]:
                raise ValueError("spec differs from existing recording")
            quality = json.loads((self.output / "completeness-report.json").read_text())
            bounds = quality["source_block_range"]
            for name, value in [
                ("from_block", bounds["from"]),
                ("to_block", bounds["to"]),
            ]:
                if identity[name] is not None and identity[name] != value:
                    raise ValueError(
                        "requested block range differs from existing recording"
                    )
                identity[name] = value
            identity["spec_sha256"] = recorded.get("spec_sha256")
        return identity

    def _rpc(self, endpoint, artifacts, role):
        source = ResumableRpc(
            endpoint, self.timeout, artifacts, self.output / ".collection/rpc" / role
        )
        source.progress = lambda completed, reused, retries: self.progress(
            f"{role}: completed={completed} reused={reused} retries={retries}; discovering total work"
        )
        return source

    def _base(self):
        if self.journal.completed("base"):
            self.progress("Base evidence: completed=1/1 reused=1 missing=0")
            return
        if (self.output / "completeness-report.json").exists():
            files = [
                self.output / name
                for name in (
                    "metadata.json",
                    "events.jsonl",
                    "completeness-report.json",
                )
            ]
            files.extend((self.output / "raw").glob("*"))
            self.journal.complete("base", files)
            return
        ready_path = self.output / ".collection/base-ready.json"
        if ready_path.exists():
            ready = json.loads(ready_path.read_text())
            scratch = self.output / ready["directory"]
            for name, expected in ready["artifacts"].items():
                if digest(scratch / name) != expected:
                    raise ValueError("unpublished base evidence is corrupt")
        else:
            if not self.spec or not self.archive_url:
                raise ValueError(
                    "fresh collection requires --spec and GMX_ARCHIVE_RPC_URL"
                )
            scratch = self.output / ".collection" / ("base-" + uuid4().hex)
            collector = GmxCollector(
                self.spec,
                scratch,
                rpc_url=self.rpc_url,
                archive_rpc_url=self.archive_url,
                confirmations=self.confirmations,
                chunk_size=self.chunk_size,
                timeout_seconds=self.timeout,
            )
            collector.rpc = self._rpc(self.rpc_url, collector.artifacts, "base-public")
            collector.archive_rpc = self._rpc(
                self.archive_url, collector.artifacts, "base-archive"
            )
            try:
                chain_id = self.spec.get("deployment", {}).get("chain_id")
                if chain_id is not None:
                    for source in (collector.rpc, collector.archive_rpc):
                        if int(source.call("eth_chainId", []), 16) != chain_id:
                            raise ValueError(
                                "provider chain differs from recording spec"
                            )
                anchor = self.spec.get("anchor_block", {})
                if anchor.get("hash") and anchor.get("number") is not None:
                    for source in (collector.rpc, collector.archive_rpc):
                        header = source.call(
                            "eth_getBlockByNumber", [hex(anchor["number"]), False]
                        )
                        if header.get("hash", "").lower() != anchor["hash"].lower():
                            raise ValueError(
                                "provider block differs from pinned configuration anchor"
                            )
                start = (
                    self.start
                    if self.start is not None
                    else collector.resolve_window_start_block()
                )
                end = (
                    self.end
                    if self.end is not None
                    else int(self.spec["observation_window"]["end_block"])
                )
                if start > end:
                    raise ValueError("invalid collection range")
                identity = dict(
                    self.journal.state["identity"], from_block=start, to_block=end
                )
                self.journal.state["identity"] = identity
                self.journal.save()
                quality = collector.collect(start, end)
                if not quality["complete"]:
                    raise ValueError(
                        "base collection incomplete; raw work units retained for resume"
                    )
            finally:
                collector.recorder.close()
                collector.artifacts.close()
            atomic_json(
                ready_path,
                {
                    "directory": str(scratch.relative_to(self.output)),
                    "artifacts": {
                        str(p.relative_to(scratch)): digest(p)
                        for p in scratch.rglob("*")
                        if p.is_file()
                    },
                },
            )
        # Each file is published atomically; report is last. Until it exists,
        # consumers and dependent stages cannot treat the base view as complete.
        files = sorted(
            p
            for p in scratch.rglob("*")
            if p.is_file() and p.name != "completeness-report.json"
        )
        files.append(scratch / "completeness-report.json")
        published = []
        for source in files:
            target = self.output / source.relative_to(scratch)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if digest(target) != digest(source):
                    raise ValueError("conflicting partially published base evidence")
            else:
                atomic_copy(source, target)
            published.append(target)
        self.journal.complete("base", published)
        self.progress("Base evidence: completed=1/1 reused=0 missing=0")

    def _snapshot(self, stage, archive, logs, requirements):
        name, schema = SNAPSHOTS[stage]
        target = snapshot_path(self.output, name)
        if (
            stage == "referral"
            and not target.exists()
            and (self.output / "referral-configuration.json").exists()
        ):
            target = self.output / "referral-configuration.json"
        completed = self.journal.completed(stage)
        if target.exists():
            value = json.loads(target.read_text())
            self._verify_snapshot(stage, value, schema, requirements, coverage=False)
            if self._snapshot_covers(stage, value, requirements):
                if not completed:
                    self.journal.complete(stage, [target])
                self.progress(f"{stage}: completed=1/1 reused=1 missing=0")
                return True
            # Preserve valid older evidence while collecting expanded dependencies.
            target = self.output / ("v2-" + name)
            if target.exists():
                raise ValueError(f"{stage} supplement lacks required dependencies")
        if archive is None:
            self.progress(f"{stage}: completed=0/1 reused=0 missing=1")
            return False
        else:
            functions = {
                "impact": lambda: fetch_opening_impact_factors(
                    self.output, archive.call
                ),
                "fees": lambda: fetch_opening_fee_configuration(
                    self.output, archive.call
                ),
                "accrual": lambda: collect_accrual(self.output, archive),
                "referral": lambda: collect_referral(
                    self.output,
                    archive,
                    logs,
                    progress=self.progress,
                    requirements=requirements,
                ),
                "swaps": lambda: collect_swaps(
                    self.output, archive, requirements=requirements
                ),
            }
            value = functions[stage]()
            self._verify_snapshot(stage, value, schema, requirements)
            value["raw_work_units_directory"] = ".collection/rpc/archive"
            atomic_json(target, value)
        self.journal.complete(stage, [target])
        self.progress(
            f"{stage}: completed=1/1 reused=0 retries={getattr(archive, 'retries', 0)} missing=0"
        )
        return True

    def _verify_snapshot(self, stage, value, schema, requirements, *, coverage=True):
        metadata = json.loads((self.output / "metadata.json").read_text())
        if value.get("schema") != schema or value.get("version") != 1:
            raise ValueError(f"incompatible {stage} snapshot")
        if value.get("market") != metadata["market"]["market_token_address"].lower():
            raise ValueError(f"{stage} snapshot market mismatch")
        store = metadata.get("contracts", {}).get("data_store")
        if store and value.get("data_store") != store.lower():
            raise ValueError(f"{stage} snapshot datastore mismatch")
        quality = json.loads((self.output / "completeness-report.json").read_text())
        opening = quality["source_block_range"]["from"] - 1
        expected = _recorded_block_hash(self.output, opening)
        point = value.get("opening", {}) if stage in {"fees", "accrual"} else value
        block = point.get("block_number", point.get("opening_block"))
        block_hash = point.get("block_hash", point.get("opening_hash"))
        if block != opening or block_hash != expected:
            raise ValueError(f"{stage} snapshot block mismatch")
        if coverage and not self._snapshot_covers(stage, value, requirements):
            raise ValueError(f"{stage} snapshot lacks required dependencies")

    @staticmethod
    def _snapshot_covers(stage, value, requirements):
        if stage == "referral":
            return requirements.traders <= set(value.get("traders", []))
        if stage == "swaps":
            return requirements.markets <= set(
                value.get("markets", [])
            ) and requirements.ui_receivers <= set(value.get("receivers", []))
        return True

    def _traces(self, archive, requirements):
        missing = []
        store = TraceStore(self.output)
        total = len(requirements.traces)
        reused = 0
        for index, requirement in enumerate(requirements.traces, 1):
            transaction = requirement.transaction_hash
            unit = "trace:" + transaction
            complete = self.journal.completed(unit)
            trace = store.get("trace", transaction)
            if trace is None:
                if complete:
                    raise ValueError("journaled trace absent from store")
                if archive is None:
                    missing.append(unit)
                    continue
                trace = collect_transaction(
                    archive,
                    transaction,
                    requirement.block.number,
                    requirement.block.hash,
                )
                self._verify_trace_identity(trace, requirement)
                trace_digest = store.put("trace", transaction, trace)
            else:
                reused += 1
                self._verify_trace_identity(trace, requirement)
                trace_digest = None
            if not complete:
                if trace_digest is None:
                    from gmx_crypto_bot_v2.evidence.trace_store import sha256

                    trace_digest = sha256(store.get_bytes("trace", transaction))
                self.journal.complete_store(unit, "trace", transaction, trace_digest)
            needs_probe = not self._supported_gas_profile(trace)
            if self.gas_probes or needs_probe:
                gas_unit = "gas:" + transaction
                gas_complete = self.journal.completed(gas_unit)
                gas = store.get("gas", transaction)
                if gas is None:
                    if gas_complete:
                        raise ValueError("journaled gas probe absent from store")
                    if archive is None:
                        missing.append(gas_unit)
                        continue
                    gas = collect_gas_probe(archive, transaction, trace)
                    gas_digest = store.put("gas", transaction, gas)
                else:
                    gas_digest = None
                if (
                    gas.get("transaction_hash") != transaction
                    or gas.get("version") != 2
                ):
                    raise ValueError("gas probe identity mismatch")
                if not gas_complete:
                    if gas_digest is None:
                        from gmx_crypto_bot_v2.evidence.trace_store import sha256

                        gas_digest = sha256(store.get_bytes("gas", transaction))
                    self.journal.complete_store(
                        gas_unit, "gas", transaction, gas_digest
                    )
            if needs_probe and store.get("gas", transaction) is None:
                missing.append("unsupported_gas_profile:" + transaction)
            if index % 100 == 0 or index == total:
                self.progress(
                    f"Traces: completed={index}/{total} reused={reused} retries={getattr(archive, 'retries', 0)} missing={len(missing)}"
                )
        return missing

    @staticmethod
    def _supported_gas_profile(trace):
        from gmx_crypto_bot_v2.domain.gas import CALIBRATED_GAS_PROFILE
        from gmx_crypto_bot_v2.domain.payments import walk_trace

        frames = {node.path: node.frame for node in walk_trace(trace.get("trace", {}))}
        for payment in trace.get("payments", []):
            if int(payment["input"]["execution_fee"]) <= 0:
                continue
            frame = frames.get(tuple(payment["path"]), {})
            if (
                len(frame.get("input", "").removeprefix("0x")) // 2
                != CALIBRATED_GAS_PROFILE["calldata_bytes"]
            ):
                return False
            code = trace.get("library_code", {}).get(payment["library"], "0x")
            if (
                hashlib.sha256(bytes.fromhex(code.removeprefix("0x"))).hexdigest()
                != CALIBRATED_GAS_PROFILE["runtime_sha256"]
            ):
                return False
        return True

    @staticmethod
    def _verify_trace_identity(trace, requirement):
        from gmx_crypto_bot_v2.domain.payments import PAY_SELECTOR, walk_trace

        transaction_hash = requirement.transaction_hash
        if (
            trace.get("schema") != "GmxExecutionFeeTrace"
            or trace.get("version") != 1
            or trace.get("transaction_hash") != transaction_hash
            or trace.get("block_number") != requirement.block.number
            or trace.get("block_hash") != requirement.block.hash
        ):
            raise ValueError("trace identity mismatch")
        receipt, transaction, root = (
            trace.get("receipt", {}),
            trace.get("transaction", {}),
            trace.get("trace", {}),
        )
        for item, field in ((receipt, "transactionHash"), (transaction, "hash")):
            if (
                item.get(field) != transaction_hash
                or item.get("blockHash") != requirement.block.hash
                or item.get("blockNumber") != hex(requirement.block.number)
            ):
                raise ValueError("trace transaction or receipt identity mismatch")
        if receipt.get("status") != "0x1" or root.get("error"):
            raise ValueError("trace transaction reverted")
        if any(
            root.get(name) != transaction.get(name) for name in ("from", "to", "input")
        ):
            raise ValueError("trace root differs from transaction")
        paths = [tuple(payment["path"]) for payment in trace.get("payments", [])]
        actual = {
            node.path
            for node in walk_trace(root)
            if node.committed and node.frame.get("input", "").startswith(PAY_SELECTOR)
        }
        if len(paths) != len(set(paths)) or set(paths) != actual:
            raise ValueError(
                "trace payment index is missing or duplicates committed calls"
            )
