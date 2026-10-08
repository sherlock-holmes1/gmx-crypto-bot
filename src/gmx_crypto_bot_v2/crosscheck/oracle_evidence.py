"""Transaction-scoped oracle evidence from immutable recorded GMX logs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from gmx_crypto_bot_v2.domain.entries import _decode_recorded_log, _event_entry, decoded_log
from gmx_crypto_bot_v2.crosscheck.router_preflight import OracleInput, encode_simulation, _key, _selector
from gmx_crypto_bot_v2.domain.keys import keccak256


def verify_historical_oracle_source(record_path: Path, code_proof_path: Path) -> dict:
    """Bind the historical Oracle source to an observed pinned runtime hash."""
    raw = record_path.read_bytes()
    record = json.loads(raw)
    proof = json.loads(code_proof_path.read_text())
    if proof.get("schema") != "GmxStep41PinnedOracleCode" or \
            record.get("runtimeMatch") != "exact_match" or record.get("match") != "exact_match" or \
            record.get("address", "").lower() != proof.get("address") or \
            str(record.get("chainId")) != str(proof.get("chain_id")) or \
            proof.get("sourcify_response_sha256") != "0x" + hashlib.sha256(raw).hexdigest():
        raise ValueError("historical Oracle source identity mismatch")
    runtime = bytes.fromhex(record["runtimeBytecode"]["onchainBytecode"][2:])
    transcript = proof.get("rpc_transcript")
    methods = [x.get("method") for x in transcript] if isinstance(transcript, list) else []
    basic = ["eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_getBlockByNumber"]
    with_age = basic + ["eth_call", "eth_getBlockByNumber"]
    if methods not in (basic, with_age) or \
            transcript[0].get("params") != [] or \
            int(transcript[0].get("response", {}).get("result", "0x0"), 16) != proof.get("chain_id") or \
            transcript[1].get("params") != [hex(proof.get("block_number", -1)), False] or \
            transcript[3].get("params") != [hex(proof.get("block_number", -1)), False] or \
            transcript[2].get("params") != [proof.get("address"), hex(proof.get("block_number", -1))] or \
            any(x.get("response", {}).get("result", {}).get("hash", "").lower() != proof.get("block_hash") or
                int(x.get("response", {}).get("result", {}).get("number", "0x0"), 16) != proof.get("block_number")
                for x in (transcript[1], transcript[3], *([transcript[5]] if methods == with_age else []))) or \
            transcript[2].get("response", {}).get("result", "").lower() != "0x" + runtime.hex():
        raise ValueError("pinned Oracle RPC transcript mismatch")
    if "0x" + keccak256(runtime).hex() != proof.get("runtime_code_keccak") or \
            "0x" + hashlib.sha256(runtime).hexdigest() != proof.get("runtime_code_sha256") or \
            not proof.get("exact_runtime_bytes_match"):
        raise ValueError("historical Oracle runtime hash mismatch")
    path = "contracts/oracle/Oracle.sol"
    source = record["sources"][path]["content"]
    source_hash = "0x" + keccak256(source.encode()).hex()
    if source_hash != record["metadata"]["sources"][path]["keccak256"]:
        raise ValueError("historical Oracle source hash mismatch")
    max_age = None
    if methods == with_age:
        manifest = json.loads((code_proof_path.parent / "historical-source-manifest.json").read_text())
        datastore = manifest["contracts"]["datastore"]["address"]
        expected_call = "0x" + (_selector("getUint(bytes32)") + _key("MAX_ORACLE_PRICE_AGE")).hex()
        actual_call = transcript[4]
        if actual_call.get("params") != [{"to": datastore, "data": expected_call},
                                          hex(proof["block_number"])] or \
                not isinstance(actual_call.get("response", {}).get("result"), str):
            raise ValueError("historical Oracle max-age call mismatch")
        max_age = int(actual_call["response"]["result"], 16)
        if max_age != proof.get("max_oracle_price_age_seconds"):
            raise ValueError("historical Oracle max-age result mismatch")
        keys_path = "contracts/data/Keys.sol"
        keys_source = record["sources"][keys_path]["content"]
        if "bytes32 public constant MAX_ORACLE_PRICE_AGE = keccak256(abi.encode(\"MAX_ORACLE_PRICE_AGE\"));" not in keys_source or \
                "0x" + keccak256(keys_source.encode()).hex() != record["metadata"]["sources"][keys_path]["keccak256"]:
            raise ValueError("historical Oracle max-age key layout unproved")
    for fragment in ('_setPrimaryPrice(validatedPrice.token',
                     'validatedPrice.min,', 'validatedPrice.max',
                     '"minPrice", minPrice', '"maxPrice", maxPrice',
                     '"timestamp", timestamp', '"OraclePriceUpdate"',
                     'validatedPrice.timestamp + maxPriceAge < Chain.currentTimestamp()'):
        if fragment not in source:
            raise ValueError("historical Oracle event mapping unproved")
    return {"oracle_source_keccak256": source_hash,
            "oracle_record_sha256": "0x" + hashlib.sha256(raw).hexdigest(),
            "pinned_code_proof_sha256": "0x" + hashlib.sha256(code_proof_path.read_bytes()).hexdigest(),
            "event_price_mapping_proved": True,
            "historical_max_price_age_rule_proved": True,
            "max_oracle_price_age_seconds": max_age,
            "raw_pinned_rpc_transcript_available": True}


def capture_recorded_oracle(recording: Path, transaction_hash: str,
                            expected_tokens: tuple[str, ...],
                            *, order_key: str | None = None,
                            order_transaction_hash: str | None = None,
                            order_event_name: str = "OrderCreated",
                            source_record: Path | None = None,
                            pinned_code_proof: Path | None = None) -> dict:
    """Capture a complete price set; disclose when it belongs to another transaction."""
    if not transaction_hash.startswith("0x") or len(transaction_hash) != 66:
        raise ValueError("invalid oracle transaction hash")
    if order_key is not None and (not order_key.startswith("0x") or len(order_key) != 66):
        raise ValueError("invalid order key")
    if order_event_name not in {"OrderCreated", "OrderExecuted"}:
        raise ValueError("unsupported order event for oracle evidence")
    if len(expected_tokens) != 2 or len({t.lower() for t in expected_tokens}) != 2:
        raise ValueError("exactly two distinct expected tokens required")
    source = recording / "events.jsonl"
    digest = hashlib.sha256()
    found = {}
    coordinates = []
    block_hashes = set()
    matching_creation = []
    with source.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            event = json.loads(raw)
            if event.get("kind") != "gmx_market_log":
                continue
            if event.get("payload", {}).get("event_name") == order_event_name and order_key:
                decoded_order = _decode_recorded_log(event)
                if decoded_order is not None and decoded_order.values.get("key", "").lower() == order_key.lower():
                    matching_creation.append(_event_entry(event, decoded_order))
                continue
            if event.get("payload", {}).get("event_name") != "OraclePriceUpdate":
                continue
            log = event["payload"].get("log", {})
            if log.get("transactionHash", "").lower() != transaction_hash.lower():
                continue
            decoded = _decode_recorded_log(event)
            if decoded is None:
                raise ValueError("oracle event cannot be decoded")
            values = _event_entry(event, decoded)["values"]
            token = values["token"].lower()
            if token not in {t.lower() for t in expected_tokens}:
                raise ValueError("unexpected oracle token in transaction")
            if token in found:
                raise ValueError("duplicate oracle token in transaction")
            low, high, timestamp = (values[k] for k in ("minPrice", "maxPrice", "timestamp"))
            if not all(isinstance(x, int) for x in (low, high, timestamp)) or \
                    low <= 0 or high < low or timestamp <= 0:
                raise ValueError("invalid oracle price or timestamp")
            found[token] = values
            block_hashes.add(log.get("blockHash", "").lower())
            coordinates.append({"block_number": event["block_number"],
                                "transaction_index": event["transaction_index"],
                                "log_index": event["log_index"],
                                "transaction_hash": transaction_hash.lower()})
    if set(found) != {t.lower() for t in expected_tokens} or len(block_hashes) != 1:
        raise ValueError("incomplete or inconsistent oracle transaction")
    if (source_record is None) != (pinned_code_proof is None):
        raise ValueError("historical Oracle source and pinned code proof must be supplied together")
    source_proof = (verify_historical_oracle_source(source_record, pinned_code_proof)
                    if source_record is not None and pinned_code_proof is not None else None)
    pin = json.loads(pinned_code_proof.read_text()) if pinned_code_proof is not None else None
    if source_proof is not None and (
            pin["block_hash"] not in block_hashes or
            pin["block_number"] != coordinates[0]["block_number"]):
        raise ValueError("Oracle source proof pin differs from recorded event block")
    timestamps = [found[t.lower()]["timestamp"] for t in expected_tokens]
    block_timestamp = (int(pin["rpc_transcript"][1]["response"]["result"]["timestamp"], 16)
                       if pin is not None else None)
    if block_timestamp is not None and any(t > block_timestamp for t in timestamps):
        raise ValueError("oracle timestamp is later than pinned block")
    max_age = source_proof.get("max_oracle_price_age_seconds") if source_proof else None
    if max_age is not None and block_timestamp - min(timestamps) > max_age:
        raise ValueError("oracle timestamp exceeds historical maximum age")
    same_transaction = (order_transaction_hash is not None and
                        order_transaction_hash.lower() == transaction_hash.lower())
    if order_key is not None and (
            order_transaction_hash is None or
            len(matching_creation) != 1 or
            matching_creation[0]["transaction_hash"] != order_transaction_hash.lower() or
            (same_transaction and any(c["log_index"] >= matching_creation[0]["log_index"]
                                      for c in coordinates))):
        raise ValueError("oracle and selected order creation relationship unproved")
    oracle = OracleInput(tuple(t.lower() for t in expected_tokens),
                         tuple((found[t.lower()]["minPrice"], found[t.lower()]["maxPrice"])
                               for t in expected_tokens), min(timestamps), max(timestamps),
                         "recorded_OraclePriceUpdate_events",
                         ("historical_Oracle_primary_price_raw_integer" if source_proof else
                          "raw_event_uint256_scale_unverified"))
    return {
        "schema": "GmxStep41RecordedOracleEvidence", "version": 1,
        "recording_events_sha256": "0x" + digest.hexdigest(),
        "oracle_transaction_hash": transaction_hash.lower(),
        "order_key": order_key.lower() if order_key else None,
        "order_event_name": order_event_name,
        "order_transaction_hash": order_transaction_hash.lower() if order_transaction_hash else None,
        "relationship": (("observed_execution_transaction_prices_preceding_order_executed"
                          if order_event_name == "OrderExecuted" else
                          "order_transaction_oracle_before_creation_not_execution_proof")
                         if same_transaction else "counterfactual_other_transaction"),
        "block_hash": next(iter(block_hashes)), "coordinates": coordinates,
        "tokens": list(oracle.tokens), "prices": [list(x) for x in oracle.prices],
        "min_timestamp": oracle.min_timestamp, "max_timestamp": oracle.max_timestamp,
        "pin_block_timestamp": block_timestamp,
        "max_price_age_seconds_at_pin": (block_timestamp - oracle.min_timestamp
                                         if block_timestamp is not None else None),
        "age_valid_without_config_read": (block_timestamp == oracle.min_timestamp
                                          if block_timestamp is not None else False),
        "max_oracle_price_age_seconds": max_age,
        "age_within_historical_max": (block_timestamp - oracle.min_timestamp <= max_age
                                      if block_timestamp is not None and max_age is not None else
                                      block_timestamp == oracle.min_timestamp),
        "provenance": oracle.provenance, "scale_proof": oracle.scale_proof,
        "source_and_scale_verified": source_proof is not None,
        "historical_source_proof": source_proof,
        "router_calldata": encode_simulation(oracle),
        "ready_for_observed_execution_comparison": False,
    }


def capture_watched_creation_oracle(rpc, candidate: dict, emitter: str,
                                    oracle_address: str, source_record: Path,
                                    expected_tokens: tuple[str, str]) -> dict:
    """Read one watched creation block; only same-tx earlier oracle events qualify."""
    creation = candidate["creation"]
    block = creation["block_number"]
    block_tag = hex(block)
    def result(method, params):
        envelope = rpc.request(method, params)
        if not isinstance(envelope, dict) or "error" in envelope or "result" not in envelope:
            raise ValueError(f"watch {method} failed")
        return envelope["result"]
    if int(result("eth_chainId", []), 16) != 42161:
        raise ValueError("watch chain mismatch")
    first = result("eth_getBlockByNumber", [block_tag, False])
    if first.get("hash", "").lower() != creation["block_hash"].lower() or \
            int(first.get("number", "0x0"), 16) != block:
        raise ValueError("watch creation block hash mismatch")
    logs = result("eth_getLogs", [{"address": emitter, "fromBlock": block_tag,
                                   "toBlock": block_tag}])
    if not isinstance(logs, list):
        raise ValueError("watch oracle logs missing")
    code = result("eth_getCode", [oracle_address, block_tag])
    raw_source = source_record.read_bytes()
    source = json.loads(raw_source)
    verify_historical_oracle_source(source_record, source_record.parent.parent / "pinned-oracle-code.json")
    if source.get("runtimeMatch") != "exact_match" or source.get("match") != "exact_match" or \
            source.get("address", "").lower() != oracle_address.lower() or \
            source.get("runtimeBytecode", {}).get("onchainBytecode", "").lower() != code.lower():
        raise ValueError("watch Oracle code differs from historical verified source")
    prices = {}
    coordinates = []
    for log in logs:
        if not isinstance(log, dict):
            raise ValueError("malformed watched event log")
        if log.get("address", "").lower() != emitter.lower() or \
                log.get("blockHash", "").lower() != creation["block_hash"].lower():
            raise ValueError("watch oracle log block or emitter mismatch")
        if log.get("transactionHash", "").lower() != creation["transaction_hash"].lower():
            continue
        try:
            decoded = decoded_log(log)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("malformed watched event log") from error
        if decoded is None:
            continue
        if decoded.event_name != "OraclePriceUpdate":
            continue
        if int(log["logIndex"], 16) >= creation["log_index"]:
            raise ValueError("watch oracle update does not precede order creation")
        values = decoded.values
        token = values["token"].lower()
        if token not in {t.lower() for t in expected_tokens} or token in prices:
            raise ValueError("unexpected or duplicate watched oracle token")
        prices[token] = values
        coordinates.append([block, int(log["transactionIndex"], 16), int(log["logIndex"], 16)])
    if set(prices) != {t.lower() for t in expected_tokens}:
        raise ValueError("incomplete watched oracle price set")
    timestamps = [prices[t.lower()]["timestamp"] for t in expected_tokens]
    block_timestamp = int(first["timestamp"], 16)
    if any(t != block_timestamp for t in timestamps):
        raise ValueError("watched oracle timestamp not equal to creation block timestamp")
    oracle = OracleInput(tuple(t.lower() for t in expected_tokens),
                         tuple((prices[t.lower()]["minPrice"], prices[t.lower()]["maxPrice"])
                               for t in expected_tokens), min(timestamps), max(timestamps),
                         "watched_OraclePriceUpdate_events", "historical_Oracle_primary_price_raw_integer")
    last = result("eth_getBlockByNumber", [block_tag, False])
    if last.get("hash", "").lower() != creation["block_hash"].lower() or \
            int(last.get("number", "0x0"), 16) != block:
        raise ValueError("watch block changed during oracle capture")
    return {"order_key": candidate["order_key"], "relationship":
            "order_transaction_oracle_before_creation_not_execution_proof",
            "tokens": list(oracle.tokens), "prices": [list(x) for x in oracle.prices],
            "min_timestamp": oracle.min_timestamp, "max_timestamp": oracle.max_timestamp,
            "router_calldata": encode_simulation(oracle), "coordinates": coordinates,
            "block_number": block, "block_hash": creation["block_hash"],
            "oracle_code_hash": "0x" + keccak256(bytes.fromhex(code[2:])).hex(),
            "source_response_sha256": "0x" + hashlib.sha256(raw_source).hexdigest(),
            "source_and_scale_verified": True,
            "ready_for_observed_execution_comparison": False}
