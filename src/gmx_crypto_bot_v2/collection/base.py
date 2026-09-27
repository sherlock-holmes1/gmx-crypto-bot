"""Capture the fixed recording window, opening checkpoint and public observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.collection.checkpoint import OpeningCheckpointCollector
from gmx_crypto_bot_v2.collection.ranges import RangeGap, adaptive_ranges
from gmx_crypto_bot_v2.domain.events import EventDecodeError, decode_event_log
from gmx_crypto_bot_v2.domain.filtering import (
    contains_address,
    event_name,
    target_snapshot,
)
from gmx_crypto_bot_v2.evidence.artifacts import (
    DEFAULT_MAX_BUNDLE_BYTES,
    RawArtifactStore,
)
from gmx_crypto_bot_v2.evidence.recording import JsonlRecorder
from gmx_crypto_bot_v2.sources.http import fetch_json
from gmx_crypto_bot_v2.sources.rpc import (
    PublicJsonRpc,
    SourceError,
    hex_block,
    parse_hex_number,
    redact_rpc_endpoint,
    utc_now,
)

DEFAULT_CONFIRMATIONS = 64


DEFAULT_CHUNK_SIZE = 5_000


ORDER_LIFECYCLE_EVENTS = {
    "OrderExecuted",
    "OrderCancelled",
    "OrderFrozen",
    "OrderUpdated",
    "OrderSizeDeltaAutoUpdated",
    "OrderCollateralDeltaAmountAutoUpdated",
}


class GmxCollector:
    """Collect public GMX contract logs and public state without decoding them prematurely."""

    def __init__(
        self,
        spec: dict[str, Any],
        output: Path,
        *,
        rpc_url: str | None = None,
        archive_rpc_url: str | None = None,
        confirmations: int = DEFAULT_CONFIRMATIONS,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout_seconds: int = 30,
    ) -> None:
        self.spec = spec
        self.output = output
        self.confirmations = confirmations
        self.chunk_size = chunk_size
        self.timeout_seconds = timeout_seconds
        self.gaps: list[RangeGap] = []
        self.reorgs: list[dict[str, Any]] = []
        self._headers: dict[int, dict[str, Any]] = {}
        self._receipt_hashes: set[str] = set()
        self._event_count = 0
        self._source_log_count = 0
        self._events_by_scope: dict[str, int] = {}
        self._target_order_keys: set[str] = set()
        self._opening_checkpoint: dict[str, Any] | None = None
        endpoint = rpc_url or spec["anchor_block"]["rpc_url"]
        self._watched_contracts = {
            role: address.lower()
            for role, address in spec["contracts"].items()
            if role
            in {
                "event_emitter",
                "data_store",
                "oracle",
                "order_handler",
                "liquidation_handler",
            }
        }
        metadata = {
            "schema": "GmxRecording",
            "version": 1,
            "status": "collecting",
            "read_only": True,
            "captured_at_utc": utc_now(),
            "spec_sha256": hashlib.sha256(
                json.dumps(spec, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "market": spec["deployment"],
            "tokens": spec["tokens"],
            "contracts": self._watched_contracts,
            "rpc_endpoint": redact_rpc_endpoint(endpoint),
            "archive_rpc_endpoint": (
                None
                if archive_rpc_url is None
                else redact_rpc_endpoint(archive_rpc_url)
            ),
            "opening_checkpoint_policy": "start_block_minus_one",
            "pinned_observation_window": spec.get("observation_window"),
            "pinned_configuration_anchor_block": spec.get("anchor_block", {}).get(
                "number"
            ),
            "pinned_configuration_raw": spec.get("configuration_raw"),
            "confirmation_depth": confirmations,
            "raw_first": True,
            "raw_artifact_directory": "raw",
            "raw_artifact_format": "rotated compressed JSONL bundles with base64-encoded response bodies",
            "raw_artifact_max_bundle_bytes": DEFAULT_MAX_BUNDLE_BYTES,
            "replay_filter": "target market, target configuration, and WETH/USDC oracle updates",
        }
        self.recorder = JsonlRecorder(output, metadata)
        self.artifacts = RawArtifactStore(output)
        self.rpc = PublicJsonRpc(endpoint, timeout_seconds, self.artifacts)
        self.archive_rpc = (
            None
            if archive_rpc_url is None
            else PublicJsonRpc(archive_rpc_url, timeout_seconds, self.artifacts)
        )
        self._market_address = spec["deployment"]["market_token_address"].lower()
        self._oracle_token_addresses = {
            spec["tokens"]["index"]["address"].lower(),
            spec["tokens"]["long"]["address"].lower(),
            spec["tokens"]["short"]["address"].lower(),
        }

    def collect(self, start_block: int, end_block: int) -> dict[str, Any]:
        try:
            latest_head = parse_hex_number(self.rpc.call("eth_blockNumber", []))
            finalized_head = latest_head - self.confirmations
            if end_block > finalized_head:
                raise ValueError(
                    f"requested end block {end_block} is newer than finalized head {finalized_head} "
                    f"at {self.confirmations} confirmations"
                )
            self.recorder.record(
                "finalized_head",
                {
                    "latest_head": latest_head,
                    "finalized_head": finalized_head,
                    "confirmations": self.confirmations,
                },
                block_number=finalized_head,
            )
            if self.archive_rpc is not None:
                self._collect_opening_checkpoint(start_block)
            self._record_header(start_block, "range_start")
            self._record_header(end_block, "range_end")
            self._collect_http_snapshots()
            for role, address in self._watched_contracts.items():
                self._collect_contract_logs(role, address, start_block, end_block)
            report = self._write_report(
                start_block, end_block, latest_head, finalized_head
            )
            return report
        finally:
            self.recorder.close()
            self.artifacts.close()

    def _collect_opening_checkpoint(self, start_block: int) -> None:
        if start_block <= 0:
            raise ValueError("cannot capture an opening checkpoint before block zero")
        checkpoint_block = start_block - 1
        checkpoint = OpeningCheckpointCollector(self.archive_rpc, self.spec).capture(
            checkpoint_block
        )
        canonical_header = self._header(checkpoint_block)
        canonical_hash = str(canonical_header.get("hash", "")).lower()
        if checkpoint["block_hash"] != canonical_hash:
            raise SourceError(
                "archive RPC checkpoint block hash does not match the canonical collection RPC: "
                f"{checkpoint['block_hash']} != {canonical_hash}"
            )
        self._opening_checkpoint = checkpoint
        self._target_order_keys.update(str(key).lower() for key in checkpoint["orders"])
        self.recorder.record(
            "opening_state_checkpoint",
            checkpoint,
            block_number=checkpoint_block,
            transaction_index=-1,
            log_index=-1,
        )

    def resolve_window_start_block(self) -> int:
        """Resolve the spec's timestamp rule by binary-searching canonical headers."""
        window = self.spec["observation_window"]
        if "start_block" in window:
            return int(window["start_block"])
        target = datetime.fromisoformat(window["start_utc"].replace("Z", "+00:00"))
        target_timestamp = int(target.timestamp())
        low = 0
        high = int(window["end_block"])
        while low < high:
            midpoint = (low + high) // 2
            timestamp = parse_hex_number(self._header(midpoint)["timestamp"])
            if timestamp < target_timestamp:
                low = midpoint + 1
            else:
                high = midpoint
        self._record_header(low, "observation_window_start_resolved")
        return low

    def _collect_contract_logs(
        self, role: str, address: str, start_block: int, end_block: int
    ) -> None:
        for chunk_start in range(start_block, end_block + 1, self.chunk_size):
            chunk_end = min(chunk_start + self.chunk_size - 1, end_block)

            def fetch(range_start: int, range_end: int) -> list[dict[str, Any]]:
                return self.rpc.call(
                    "eth_getLogs",
                    [
                        {
                            "address": address,
                            "fromBlock": hex_block(range_start),
                            "toBlock": hex_block(range_end),
                        }
                    ],
                )

            for range_start, range_end, logs in adaptive_ranges(
                fetch,
                chunk_start,
                chunk_end,
                self._record_gap,
                source=f"eth_getLogs:{role}",
            ):
                self._source_log_count += len(logs)
                self.recorder.record(
                    "log_range",
                    {
                        "contract_role": role,
                        "address": address,
                        "from_block": range_start,
                        "to_block": range_end,
                        "source_log_count": len(logs),
                    },
                    block_number=range_end,
                )
                for log in sorted(
                    logs,
                    key=lambda item: (
                        parse_hex_number(item["blockNumber"]),
                        parse_hex_number(item["transactionIndex"]),
                        parse_hex_number(item["logIndex"]),
                    ),
                ):
                    scope = self._log_scope(role, log)
                    if scope:
                        self._record_log(role, address, log, scope)

    def _record_log(
        self, role: str, address: str, log: dict[str, Any], scope: str
    ) -> None:
        block_number = parse_hex_number(log["blockNumber"])
        transaction_index = parse_hex_number(log["transactionIndex"])
        log_index = parse_hex_number(log["logIndex"])
        self.recorder.record(
            "gmx_market_log",
            {
                "scope": scope,
                "event_name": event_name(log),
                "contract_role": role,
                "address": address,
                "log": log,
            },
            block_number=block_number,
            transaction_index=transaction_index,
            log_index=log_index,
        )
        self._event_count += 1
        self._events_by_scope[scope] = self._events_by_scope.get(scope, 0) + 1
        try:
            header = self._header(block_number)
        except SourceError as error:
            self._record_gap(
                RangeGap("eth_getBlockByNumber", block_number, block_number, str(error))
            )
        else:
            observed_hash = log.get("blockHash", "").lower()
            canonical_hash = header.get("hash", "").lower()
            if observed_hash != canonical_hash:
                reorg = {
                    "block_number": block_number,
                    "contract_role": role,
                    "log_block_hash": observed_hash,
                    "canonical_block_hash": canonical_hash,
                    "transaction_hash": log.get("transactionHash"),
                }
                self.reorgs.append(reorg)
                self.recorder.record(
                    "reorg_detected",
                    reorg,
                    block_number=block_number,
                    transaction_index=transaction_index,
                    log_index=log_index,
                )
        transaction_hash = log.get("transactionHash", "").lower()
        if transaction_hash and transaction_hash not in self._receipt_hashes:
            self._receipt_hashes.add(transaction_hash)
            self._record_receipt(transaction_hash, block_number, transaction_index)

    def _log_scope(self, role: str, log: dict[str, Any]) -> str | None:
        """Keep only evidence needed to reconstruct the pinned market."""
        if role == "event_emitter":
            try:
                decoded = decode_event_log(log["data"])
            except (EventDecodeError, KeyError):
                decoded = None
            if decoded:
                values = decoded.values
                if "market" in values:
                    if values["market"] != self._market_address:
                        return None
                    if decoded.event_name == "OrderCreated" and isinstance(
                        values.get("key"), str
                    ):
                        self._target_order_keys.add(values["key"].lower())
                    return "market"
                if decoded.event_name in ORDER_LIFECYCLE_EVENTS and isinstance(
                    values.get("key"), str
                ):
                    if values["key"].lower() in self._target_order_keys:
                        return "order_lifecycle"
        if contains_address(log, self._market_address):
            return "market" if role == "event_emitter" else "market_configuration"
        if role == "event_emitter" and event_name(log) == "OraclePriceUpdate":
            if any(
                contains_address(log, token) for token in self._oracle_token_addresses
            ):
                return "oracle_dependency"
        return None

    def _record_receipt(
        self, transaction_hash: str, block_number: int, transaction_index: int
    ) -> None:
        try:
            receipt = self.rpc.call("eth_getTransactionReceipt", [transaction_hash])
        except SourceError as error:
            self._record_gap(
                RangeGap(
                    "eth_getTransactionReceipt", block_number, block_number, str(error)
                )
            )
            return
        self.recorder.record(
            "transaction_receipt",
            {
                "transaction_hash": transaction_hash,
                "status": receipt.get("status"),
                "gas_used": receipt.get("gasUsed"),
                "effective_gas_price": receipt.get("effectiveGasPrice"),
            },
            block_number=block_number,
            transaction_index=transaction_index,
        )

    def _header(self, block_number: int) -> dict[str, Any]:
        if block_number not in self._headers:
            header = self.rpc.call(
                "eth_getBlockByNumber", [hex_block(block_number), False]
            )
            if header is None:
                raise SourceError(
                    f"eth_getBlockByNumber: block {block_number} returned null"
                )
            self._headers[block_number] = header
        return self._headers[block_number]

    def _record_header(self, block_number: int, reason: str) -> None:
        header = self._header(block_number)
        self.recorder.record(
            "block_header",
            {
                "reason": reason,
                "number": header.get("number"),
                "hash": header.get("hash"),
                "parent_hash": header.get("parentHash"),
                "timestamp": header.get("timestamp"),
            },
            block_number=block_number,
        )

    def _record_gap(self, gap: RangeGap) -> None:
        self.gaps.append(gap)
        self.recorder.record("data_gap", asdict(gap), block_number=gap.from_block)

    def _collect_http_snapshots(self) -> None:
        for name, url in self.spec.get("source_urls", {}).items():
            if name in {"contract_addresses", "api_integration_guide", "arbitrum_rpc"}:
                continue
            try:
                payload = fetch_json(
                    url, f"http-{name}", self.artifacts, self.timeout_seconds
                )
            except SourceError as error:
                self._record_gap(RangeGap(f"http_snapshot:{name}", 0, 0, str(error)))
                continue
            self.recorder.record(
                "market_snapshot",
                {
                    "source": name,
                    "url": redact_rpc_endpoint(url),
                    "market": target_snapshot(payload, self._market_address),
                    "observed_at_utc": utc_now(),
                },
            )

    def _write_report(
        self, start_block: int, end_block: int, latest_head: int, finalized_head: int
    ) -> dict[str, Any]:
        report = {
            "schema": "GmxRecordingCompletenessReport",
            "version": 1,
            "created_at_utc": utc_now(),
            "source_block_range": {"from": start_block, "to": end_block},
            "latest_head": latest_head,
            "finalized_head": finalized_head,
            "events_recorded": self._event_count,
            "source_logs_seen": self._source_log_count,
            "events_by_scope": self._events_by_scope,
            "receipts_recorded": len(self._receipt_hashes),
            "headers_recorded": len(self._headers),
            "opening_checkpoint": (
                None
                if self._opening_checkpoint is None
                else {
                    "block_number": self._opening_checkpoint["block_number"],
                    "block_hash": self._opening_checkpoint["block_hash"],
                    "orders": len(self._opening_checkpoint["orders"]),
                    "positions": len(self._opening_checkpoint["positions"]),
                }
            ),
            "gaps": [asdict(gap) for gap in self.gaps],
            "reorgs": self.reorgs,
            "complete": not self.gaps
            and not self.reorgs
            and self._opening_checkpoint is not None,
        }
        (self.output / "completeness-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report


def load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("schema") != "GmxMarketSpec":
        raise ValueError(f"{path} is not a GmxMarketSpec")
    return spec
