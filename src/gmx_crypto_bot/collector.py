"""Read-only, raw-first collector for bounded GMX V2 Arbitrum recordings."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from gmx_crypto_bot.recording import JsonlRecorder

DEFAULT_CONFIRMATIONS = 64
DEFAULT_CHUNK_SIZE = 5_000
USER_AGENT = "gmx-crypto-bot/0.1 read-only-research"


class SourceError(RuntimeError):
    """A public source did not return a usable response."""


@dataclass(frozen=True)
class RangeGap:
    source: str
    from_block: int
    to_block: int
    error: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hex_block(number: int) -> str:
    return hex(number)


def parse_hex_number(value: str) -> int:
    return int(value, 16)


class PublicJsonRpc:
    """Small JSON-RPC client that has no wallet, account, or signing surface."""

    def __init__(self, endpoint: str, timeout_seconds: int, recorder: JsonlRecorder) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.recorder = recorder
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        request_payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        request_body = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=request_body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
                response_payload = json.loads(raw_body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            self.recorder.record("rpc_error", {"endpoint": self.endpoint, "request": request_payload, "error": str(error)})
            raise SourceError(f"{method}: {error}") from error

        # This is deliberately recorded before callers derive a block, log, or receipt event.
        self.recorder.record(
            "rpc_response",
            {"endpoint": self.endpoint, "request": request_payload, "response": response_payload},
        )
        if response_payload.get("error"):
            raise SourceError(f"{method}: {response_payload['error']}")
        if "result" not in response_payload:
            raise SourceError(f"{method}: response has neither result nor error")
        return response_payload["result"]


def adaptive_ranges(
    fetch: Callable[[int, int], list[dict[str, Any]]],
    start: int,
    end: int,
    on_gap: Callable[[RangeGap], None],
    *,
    source: str,
) -> Iterator[tuple[int, int, list[dict[str, Any]]]]:
    """Split rejected log ranges until each readable range or explicit one-block gap."""
    try:
        yield start, end, fetch(start, end)
        return
    except SourceError as error:
        if start == end:
            on_gap(RangeGap(source, start, end, str(error)))
            return
    midpoint = start + (end - start) // 2
    yield from adaptive_ranges(fetch, start, midpoint, on_gap, source=source)
    yield from adaptive_ranges(fetch, midpoint + 1, end, on_gap, source=source)


class GmxCollector:
    """Collect public GMX contract logs and public state without decoding them prematurely."""

    def __init__(
        self,
        spec: dict[str, Any],
        output: Path,
        *,
        rpc_url: str | None = None,
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
        endpoint = rpc_url or spec["anchor_block"]["rpc_url"]
        self._watched_contracts = {
            role: address.lower()
            for role, address in spec["contracts"].items()
            if role in {"event_emitter", "data_store", "oracle", "order_handler", "liquidation_handler"}
        }
        metadata = {
            "schema": "GmxRecording",
            "version": 1,
            "status": "collecting",
            "read_only": True,
            "captured_at_utc": utc_now(),
            "spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest(),
            "market": spec["deployment"],
            "contracts": self._watched_contracts,
            "rpc_endpoint": endpoint,
            "pinned_observation_window": spec.get("observation_window"),
            "pinned_configuration_anchor_block": spec.get("anchor_block", {}).get("number"),
            "pinned_configuration_raw": spec.get("configuration_raw"),
            "confirmation_depth": confirmations,
            "raw_first": True,
        }
        self.recorder = JsonlRecorder(output, metadata)
        self.rpc = PublicJsonRpc(endpoint, timeout_seconds, self.recorder)

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
                {"latest_head": latest_head, "finalized_head": finalized_head, "confirmations": self.confirmations},
                block_number=finalized_head,
            )
            self._record_header(start_block, "range_start")
            self._record_header(end_block, "range_end")
            self._collect_http_snapshots()
            for role, address in self._watched_contracts.items():
                self._collect_contract_logs(role, address, start_block, end_block)
            report = self._write_report(start_block, end_block, latest_head, finalized_head)
            return report
        finally:
            self.recorder.close()

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

    def _collect_contract_logs(self, role: str, address: str, start_block: int, end_block: int) -> None:
        for chunk_start in range(start_block, end_block + 1, self.chunk_size):
            chunk_end = min(chunk_start + self.chunk_size - 1, end_block)

            def fetch(range_start: int, range_end: int) -> list[dict[str, Any]]:
                return self.rpc.call(
                    "eth_getLogs",
                    [{"address": address, "fromBlock": hex_block(range_start), "toBlock": hex_block(range_end)}],
                )

            for range_start, range_end, logs in adaptive_ranges(
                fetch, chunk_start, chunk_end, self._record_gap, source=f"eth_getLogs:{role}"
            ):
                self.recorder.record(
                    "log_range",
                    {"contract_role": role, "address": address, "from_block": range_start, "to_block": range_end, "log_count": len(logs)},
                    block_number=range_end,
                )
                for log in logs:
                    self._record_log(role, address, log)

    def _record_log(self, role: str, address: str, log: dict[str, Any]) -> None:
        block_number = parse_hex_number(log["blockNumber"])
        transaction_index = parse_hex_number(log["transactionIndex"])
        log_index = parse_hex_number(log["logIndex"])
        self.recorder.record(
            "gmx_contract_log",
            {"contract_role": role, "address": address, "log": log},
            block_number=block_number,
            transaction_index=transaction_index,
            log_index=log_index,
        )
        self._event_count += 1
        try:
            header = self._header(block_number)
        except SourceError as error:
            self._record_gap(RangeGap("eth_getBlockByNumber", block_number, block_number, str(error)))
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
                self.recorder.record("reorg_detected", reorg, block_number=block_number, transaction_index=transaction_index, log_index=log_index)
        transaction_hash = log.get("transactionHash", "").lower()
        if transaction_hash and transaction_hash not in self._receipt_hashes:
            self._receipt_hashes.add(transaction_hash)
            self._record_receipt(transaction_hash, block_number, transaction_index)

    def _record_receipt(self, transaction_hash: str, block_number: int, transaction_index: int) -> None:
        try:
            receipt = self.rpc.call("eth_getTransactionReceipt", [transaction_hash])
        except SourceError as error:
            self._record_gap(RangeGap("eth_getTransactionReceipt", block_number, block_number, str(error)))
            return
        self.recorder.record(
            "transaction_receipt",
            {"transaction_hash": transaction_hash, "receipt": receipt},
            block_number=block_number,
            transaction_index=transaction_index,
        )

    def _header(self, block_number: int) -> dict[str, Any]:
        if block_number not in self._headers:
            header = self.rpc.call("eth_getBlockByNumber", [hex_block(block_number), False])
            if header is None:
                raise SourceError(f"eth_getBlockByNumber: block {block_number} returned null")
            self._headers[block_number] = header
        return self._headers[block_number]

    def _record_header(self, block_number: int, reason: str) -> None:
        header = self._header(block_number)
        self.recorder.record("block_header", {"reason": reason, "header": header}, block_number=block_number)

    def _record_gap(self, gap: RangeGap) -> None:
        self.gaps.append(gap)
        self.recorder.record("data_gap", asdict(gap), block_number=gap.from_block)

    def _collect_http_snapshots(self) -> None:
        for name, url in self.spec.get("source_urls", {}).items():
            if name in {"contract_addresses", "api_integration_guide", "arbitrum_rpc"}:
                continue
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw_body = response.read().decode("utf-8")
                    payload = json.loads(raw_body)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                self._record_gap(RangeGap(f"http_snapshot:{name}", 0, 0, str(error)))
                continue
            self.recorder.record("http_response", {"source": name, "url": url, "response": payload})
            self.recorder.record("market_snapshot", {"source": name, "url": url, "observed_at_utc": utc_now()})

    def _write_report(self, start_block: int, end_block: int, latest_head: int, finalized_head: int) -> dict[str, Any]:
        report = {
            "schema": "GmxRecordingCompletenessReport",
            "version": 1,
            "created_at_utc": utc_now(),
            "source_block_range": {"from": start_block, "to": end_block},
            "latest_head": latest_head,
            "finalized_head": finalized_head,
            "events_recorded": self._event_count,
            "receipts_recorded": len(self._receipt_hashes),
            "headers_recorded": len(self._headers),
            "gaps": [asdict(gap) for gap in self.gaps],
            "reorgs": self.reorgs,
            "complete": not self.gaps and not self.reorgs,
        }
        (self.output / "completeness-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report


def load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("schema") != "GmxMarketSpec":
        raise ValueError(f"{path} is not a GmxMarketSpec")
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a bounded, read-only GMX V2 Arbitrum data set.")
    parser.add_argument("--spec", required=True, type=Path, help="Pinned GmxMarketSpec JSON file")
    parser.add_argument("--output", required=True, type=Path, help="New recording directory; it must not already exist")
    parser.add_argument("--from-block", type=int, help="Override the pinned observation-window start block")
    parser.add_argument("--to-block", type=int, help="Override the pinned observation-window end block")
    parser.add_argument("--rpc-url", help="Public Arbitrum JSON-RPC override")
    parser.add_argument("--confirmations", type=int, default=DEFAULT_CONFIRMATIONS)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.confirmations < 1 or args.chunk_size < 1 or args.timeout_seconds < 1:
        parser.error("confirmations, chunk size, and timeout must be positive")

    spec = load_spec(args.spec)
    window = spec["observation_window"]
    collector = GmxCollector(
        spec,
        args.output,
        rpc_url=args.rpc_url,
        confirmations=args.confirmations,
        chunk_size=args.chunk_size,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        start = args.from_block if args.from_block is not None else collector.resolve_window_start_block()
        end = args.to_block if args.to_block is not None else int(window["end_block"])
        if start > end:
            raise ValueError("from block must not be greater than to block")
        report = collector.collect(start, end)
    except (OSError, SourceError, ValueError) as error:
        print(f"collection failed: {error}", file=sys.stderr)
        return 1
    finally:
        collector.recorder.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
