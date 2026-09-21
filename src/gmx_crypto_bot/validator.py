"""Read-only observed-order lifecycle and execution validation for GMX recordings."""
from __future__ import annotations

import argparse
import base64
import gzip
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from gmx_crypto_bot.event_decoder import DecodedEventLog, EventDecodeError, decode_event_log, event_name_from_data


TERMINAL_EVENTS = {"OrderExecuted", "OrderCancelled", "OrderFrozen"}
LIFECYCLE_EVENTS = TERMINAL_EVENTS | {"OrderUpdated", "OrderSizeDeltaAutoUpdated", "OrderCollateralDeltaAmountAutoUpdated"}
POSITION_EVENTS = {"PositionIncrease", "PositionDecrease", "PositionFeesCollected"}
MAX_UINT256 = (1 << 256) - 1


@dataclass(frozen=True)
class ValidationReport:
    schema: str
    version: int
    recording: str
    orders_created: int
    terminal_orders: int
    matched: int
    mismatched: int
    unresolved: int
    decode_errors: int
    complete: bool
    mismatch_counts: dict[str, int]
    orders: list[dict[str, Any]]


def validate_orders(recording: Path) -> ValidationReport:
    """Join target order requests to raw terminal events and observed execution events."""
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    target_market = metadata["market"]["market_token_address"].lower()
    created, observed, receipts, decode_errors = _load_replay_evidence(recording, target_market)
    lifecycle, raw_decode_errors = _load_terminal_lifecycle(recording, set(created))
    decode_errors += raw_decode_errors
    results: list[dict[str, Any]] = []
    mismatch_counts: Counter[str] = Counter()

    for key, request in sorted(created.items(), key=lambda item: _coordinate(item[1])):
        order = _validate_order(key, request, lifecycle.get(key, []), observed.get(key, []), receipts)
        results.append(order)
        mismatch_counts.update(order["mismatches"])

    matched = sum(order["status"] == "matched" for order in results)
    mismatched = sum(order["status"] == "mismatch" for order in results)
    unresolved = sum(order["status"] == "unresolved" for order in results)
    return ValidationReport(
        schema="GmxObservedOrderValidationReport",
        version=1,
        recording=str(recording),
        orders_created=len(results),
        terminal_orders=len(results) - unresolved,
        matched=matched,
        mismatched=mismatched,
        unresolved=unresolved,
        decode_errors=decode_errors,
        complete=not mismatched and not unresolved and not decode_errors,
        mismatch_counts=dict(sorted(mismatch_counts.items())),
        orders=results,
    )


def _load_replay_evidence(
    recording: Path, target_market: str
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], int]:
    created: dict[str, dict[str, Any]] = {}
    observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    receipts: dict[str, dict[str, Any]] = {}
    decode_errors = 0
    with (recording / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event["kind"] == "transaction_receipt":
                receipts[event["payload"]["transaction_hash"].lower()] = event
                continue
            if event["kind"] != "gmx_market_log":
                continue
            name = event["payload"].get("event_name")
            if name not in {"OrderCreated"} | POSITION_EVENTS:
                continue
            decoded = _decode_recorded_log(event)
            if decoded is None:
                decode_errors += 1
                continue
            entry = _event_entry(event, decoded)
            if name == "OrderCreated":
                if decoded.values.get("market") != target_market:
                    continue
                key = _order_key(decoded)
                if key is None:
                    decode_errors += 1
                    continue
                created[key] = entry
            else:
                key = decoded.values.get("orderKey")
                if isinstance(key, str):
                    observed[key].append(entry)
    return created, observed, receipts, decode_errors


def _load_terminal_lifecycle(recording: Path, target_keys: set[str]) -> tuple[dict[str, list[dict[str, Any]]], int]:
    lifecycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decode_errors = 0
    for log in _iter_raw_event_emitter_logs(recording):
        name = event_name_from_data(log.get("data", ""))
        if name not in LIFECYCLE_EVENTS:
            continue
        try:
            decoded = decode_event_log(log["data"])
        except (EventDecodeError, KeyError):
            decode_errors += 1
            continue
        key = _order_key(decoded)
        if key in target_keys:
            lifecycle[key].append(_event_entry_from_log(log, decoded))
    for entries in lifecycle.values():
        entries.sort(key=_coordinate)
    return lifecycle, decode_errors


def _iter_raw_event_emitter_logs(recording: Path) -> Iterator[dict[str, Any]]:
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    emitter = metadata["contracts"]["event_emitter"].lower()
    wanted_sequences = _event_emitter_response_sequences(recording, emitter)
    for bundle in sorted((recording / "raw").glob("rpc-*.jsonl.gz")):
        with gzip.open(bundle, "rt", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record["seq"] not in wanted_sequences:
                    continue
                response = json.loads(base64.b64decode(record["body_base64"]))
                if not isinstance(response, dict):
                    continue
                logs = response.get("result")
                if not isinstance(logs, list):
                    continue
                for log in logs:
                    if isinstance(log, dict) and log.get("address", "").lower() == emitter:
                        yield log


def _event_emitter_response_sequences(recording: Path, emitter: str) -> set[int]:
    sequences: set[int] = set()
    with (recording / "raw" / "manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            if entry.get("kind") != "response" or entry.get("source") != "rpc-eth_getLogs":
                continue
            params = entry.get("request", {}).get("params", [])
            if not params or not isinstance(params[0], dict):
                continue
            if params[0].get("address", "").lower() == emitter:
                sequences.add(entry["seq"])
    return sequences


def _validate_order(
    key: str,
    request: dict[str, Any],
    lifecycle: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    receipts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    updates = [entry for entry in lifecycle if entry["event_name"] not in TERMINAL_EVENTS]
    terminals = [entry for entry in lifecycle if entry["event_name"] in TERMINAL_EVENTS]
    final_request = dict(request["values"])
    for update in updates:
        if update["event_name"] == "OrderSizeDeltaAutoUpdated":
            final_request["sizeDeltaUsd"] = update["values"]["nextSizeDeltaUsd"]
        elif update["event_name"] == "OrderCollateralDeltaAmountAutoUpdated":
            final_request["initialCollateralDeltaAmount"] = update["values"]["nextCollateralDeltaAmount"]
        else:
            final_request.update(update["values"])
    mismatches: list[str] = []
    checks: dict[str, str] = {}
    if not terminals:
        return _order_result(key, request, final_request, None, observed, checks, ["no_terminal_event_in_window"], "unresolved")
    terminal = terminals[-1]
    if len(terminals) != 1:
        mismatches.append("multiple_terminal_events")
    checks["terminal_after_request"] = _comparison(_coordinate(terminal) > _coordinate(request), "terminal_before_request", mismatches)
    checks["account"] = _comparison(
        terminal["values"].get("account") == final_request.get("account"), "terminal_account_mismatch", mismatches
    )
    if terminal["event_name"] == "OrderExecuted":
        related = observed
        position = next((entry for entry in related if entry["event_name"] in {"PositionIncrease", "PositionDecrease"}), None)
        fees = next((entry for entry in related if entry["event_name"] == "PositionFeesCollected"), None)
        if position is None:
            mismatches.append("missing_position_execution_event")
            checks["position_execution"] = "mismatch"
        else:
            checks["position_execution"] = "matched"
            _compare_execution(final_request, position, checks, mismatches)
        if fees is not None and position is not None:
            checks["fee_trade_size"] = _comparison(
                fees["values"].get("tradeSizeUsd") == position["values"].get("sizeDeltaUsd"),
                "fee_trade_size_mismatch",
                mismatches,
            )
        receipt = receipts.get(terminal["transaction_hash"])
        if receipt is None:
            checks["receipt"] = "unavailable"
        else:
            checks["receipt"] = _comparison(receipt["payload"].get("status") == "0x1", "failed_execution_receipt", mismatches)
    status = "mismatch" if mismatches else "matched"
    return _order_result(key, request, final_request, terminal, observed, checks, mismatches, status)


def _compare_execution(
    final_request: dict[str, Any], position: dict[str, Any], checks: dict[str, str], mismatches: list[str]
) -> None:
    values = position["values"]
    requested_size = final_request.get("sizeDeltaUsd")
    if requested_size == MAX_UINT256:
        checks["size_delta"] = "matched_full_position_close"
    else:
        checks["size_delta"] = _comparison(
            values.get("sizeDeltaUsd") == requested_size, "size_delta_mismatch", mismatches
        )
    acceptable = final_request.get("acceptablePrice")
    execution_price = values.get("executionPrice")
    is_long = final_request.get("isLong")
    if acceptable == MAX_UINT256 or not isinstance(acceptable, int) or acceptable == 0 or not isinstance(execution_price, int):
        checks["acceptable_price"] = "unavailable"
        return
    is_increase = position["event_name"] == "PositionIncrease"
    buying_index = bool(is_long) == is_increase
    accepted = execution_price <= acceptable if buying_index else execution_price >= acceptable
    checks["acceptable_price"] = _comparison(accepted, "acceptable_price_violation", mismatches)


def _comparison(passed: bool, mismatch: str, mismatches: list[str]) -> str:
    if passed:
        return "matched"
    mismatches.append(mismatch)
    return "mismatch"


def _order_result(
    key: str,
    request: dict[str, Any],
    final_request: dict[str, Any],
    terminal: dict[str, Any] | None,
    observed: list[dict[str, Any]],
    checks: dict[str, str],
    mismatches: list[str],
    status: str,
) -> dict[str, Any]:
    result = {
        "key": key,
        "status": status,
        "request": request,
        "final_request": final_request,
        "terminal": terminal,
        "observed_execution_events": observed,
        "checks": checks,
        "mismatches": mismatches,
    }
    if terminal is not None:
        result["request_to_terminal_blocks"] = terminal["block_number"] - request["block_number"]
    return result


def _decode_recorded_log(event: dict[str, Any]) -> DecodedEventLog | None:
    try:
        return decode_event_log(event["payload"]["log"]["data"])
    except (EventDecodeError, KeyError):
        return None


def _event_entry(event: dict[str, Any], decoded: DecodedEventLog) -> dict[str, Any]:
    log = event["payload"]["log"]
    return {
        "event_name": decoded.event_name,
        "values": decoded.values,
        "block_number": event["block_number"],
        "transaction_index": event["transaction_index"],
        "log_index": event["log_index"],
        "transaction_hash": log["transactionHash"].lower(),
    }


def _event_entry_from_log(log: dict[str, Any], decoded: DecodedEventLog) -> dict[str, Any]:
    return {
        "event_name": decoded.event_name,
        "values": decoded.values,
        "block_number": int(log["blockNumber"], 16),
        "transaction_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "transaction_hash": log["transactionHash"].lower(),
    }


def _order_key(decoded: DecodedEventLog) -> str | None:
    key = decoded.values.get("key")
    return key.lower() if isinstance(key, str) else None


def _coordinate(entry: dict[str, Any]) -> tuple[int, int, int]:
    return (int(entry["block_number"]), int(entry["transaction_index"]), int(entry["log_index"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate reconstructed GMX order outcomes against observed events.")
    parser.add_argument("recording", type=Path, help="GMX recording directory")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report to this file")
    parser.add_argument("--json", action="store_true", help="Print the complete JSON report")
    args = parser.parse_args()
    report = validate_orders(args.recording)
    result = asdict(report)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Orders created  {report.orders_created}")
        print(f"Terminal orders {report.terminal_orders}")
        print(f"Matched         {report.matched}")
        print(f"Mismatched      {report.mismatched}")
        print(f"Unresolved      {report.unresolved}")
        print(f"Decode errors   {report.decode_errors}")
        print(f"Validation      {'PASS' if report.complete else 'INCOMPLETE'}")
    return 0 if report.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
