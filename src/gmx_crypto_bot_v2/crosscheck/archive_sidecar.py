"""Targeted, read-only archive evidence for one recorded increase order."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.crosscheck.archive_state import (
    PinnedArchiveStateReader, execute_order_feature_cell,
)
from gmx_crypto_bot_v2.crosscheck.archive_referral import read_referral_at_pin
from gmx_crypto_bot_v2.crosscheck.router_preflight import (
    Deployment, RawRpc, RouterPreflight, _FIELDS, _ARRAY_FIELDS,
    _ADDRESS_FIELDS, _BOOL_FIELDS, _field_key, _hex_bytes, _key,
    latest_key, RpcFailure,
)


class EvidenceRpc:
    """Retain exact read-only requests and responses without recording an RPC URL."""

    METHODS = frozenset({"eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call"})

    def __init__(self, inner: RawRpc):
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method not in self.METHODS:
            raise ValueError("sidecar permits read-only archive methods only")
        result = self.inner.request(method, params)
        if not isinstance(result, dict):
            raise ValueError("malformed archive RPC envelope")
        # Provider errors can echo a credential-bearing endpoint. Preserve a
        # digest while redacting the human-readable error body.
        recorded = ({"error": "redacted_provider_error"} if "error" in result
                    else result)
        self.calls.append({"method": method, "params": params,
                           "response": recorded,
                           "response_sha256": "0x" + hashlib.sha256(
                               json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
                           ).hexdigest()})
        return result


def _code_hash(rpc: RouterPreflight, address: str, block: int, expected: str) -> None:
    code = rpc._rpc("eth_getCode", [address, hex(block)])
    if not isinstance(code, str) or not code.startswith("0x") or code == "0x":
        raise ValueError("missing historical deployment code")
    actual = "0x" + keccak256(bytes.fromhex(code[2:])).hex()
    if actual != expected.lower():
        raise ValueError("historical deployment code hash mismatch")


def _verified_header(rpc: RouterPreflight, block: int) -> str:
    header = rpc._rpc("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict):
        raise ValueError("missing archive block header")
    number = header.get("number")
    if not isinstance(number, str) or int(number, 16) != block:
        raise ValueError("archive block number mismatch")
    return "0x" + _hex_bytes(header.get("hash"), 32).hex()


def _on_chain_order(rpc: RouterPreflight, key: str, block: int) -> dict[str, Any]:
    key_bytes = _hex_bytes(key, 32)
    result: dict[str, Any] = {}
    for name, label in _FIELDS:
        field_key = _field_key(key_bytes, label)
        if name in _ARRAY_FIELDS:
            signature, size = _ARRAY_FIELDS[name]
            result[name] = rpc._array(signature, field_key, block, size)
        elif name in _ADDRESS_FIELDS:
            value = rpc._scalar("getAddress(bytes32)", field_key, block)
            result[name] = "0x" + value.to_bytes(32, "big")[-20:].hex()
        elif name in _BOOL_FIELDS:
            value = rpc._scalar("getBool(bytes32)", field_key, block)
            if value not in (0, 1):
                raise ValueError("malformed order boolean")
            result[name] = bool(value)
        else:
            result[name] = rpc._scalar("getUint(bytes32)", field_key, block)
    return result


def collect_increase_sidecar(
    rpc: RawRpc, deployment: Deployment, candidate: dict[str, Any], *,
    reader_address: str, reader_code_hash: str,
    index_token: str, long_token: str, short_token: str,
    order_handler: str | None = None, order_handler_code_hash: str | None = None,
    referral_storage: str | None = None,
    referral_storage_code_hash: str | None = None,
) -> dict[str, Any]:
    """Collect one pinned snapshot; never issue a router simulation or transaction.

    All failures remain in the sidecar. `ready_for_comparison` stays false because
    oracle provenance, referral terms and execution-context equivalence are open.
    """
    traced = EvidenceRpc(rpc)
    preflight = RouterPreflight(traced, deployment)
    block = candidate.get("proposed_pin_block")
    block_hash = candidate.get("proposed_pin_hash")
    key = candidate.get("order_key")
    sidecar: dict[str, Any] = {
        "schema": "GmxStep41ArchiveSidecar", "version": 1,
        "source": {"recording": candidate.get("recording"),
                   "order_key": key, "creation": candidate.get("creation"),
                   "recorded_request": candidate.get("request_at_proposed_pin")},
        "pin": {"number": block, "recorded_hash": block_hash},
        "deployment": {"chain_id": deployment.chain_id,
                       "router": deployment.router, "router_code_hash": deployment.router_code_hash,
                       "datastore": deployment.datastore,
                       "datastore_code_hash": deployment.datastore_code_hash,
                       "reader": reader_address, "reader_code_hash": reader_code_hash,
                       "order_handler": order_handler,
                       "order_handler_code_hash": order_handler_code_hash,
                       "referral_storage": referral_storage,
                       "referral_storage_code_hash": referral_storage_code_hash},
        "status": "unavailable", "ready_for_comparison": False,
        "selection_skip_reasons": candidate.get("selection_skip_reasons"),
        "missing_evidence": ["historical_source_and_key_layout_proof", "referral_terms",
                             "oracle_prices_and_timestamps", "final_token_delta",
                             "equivalent_pre_execution_context"],
    }
    try:
        if not isinstance(candidate.get("selection_skip_reasons"), list):
            raise ValueError("missing candidate selection result")
        if candidate["selection_skip_reasons"]:
            raise ValueError("recorded candidate has selection skip reasons")
        if not isinstance(block, int) or block < 0:
            raise ValueError("invalid pin block")
        expected_hash = "0x" + _hex_bytes(block_hash, 32).hex()
        key = "0x" + _hex_bytes(key, 32).hex()
        request = candidate.get("request_at_proposed_pin")
        if not isinstance(request, dict) or request.get("orderType") != 2:
            raise ValueError("candidate is not a recorded MarketIncrease")
        core = ("account", "market", "orderType", "isLong", "initialCollateralToken",
                "sizeDeltaUsd", "acceptablePrice", "triggerPrice")
        missing_core = [name for name in core if name not in request]
        if missing_core:
            raise ValueError("missing core recorded request fields: " + ",".join(missing_core))
        supported_fields = {name for name, _ in _FIELDS} | {"key"}
        unknown_fields = sorted(set(request) - supported_fields)
        if unknown_fields:
            raise ValueError("unmapped recorded request fields: " + ",".join(unknown_fields))
        if request.get("key", key).lower() != key:
            raise ValueError("recorded request key mismatch")
        sidecar["archive_only_request_fields"] = [name for name, _ in _FIELDS
                                                   if name not in request]
        if int(preflight._rpc("eth_chainId", []), 16) != deployment.chain_id:
            raise ValueError("archive chain mismatch")
        before = _verified_header(preflight, block)
        if before != expected_hash:
            raise ValueError("archive block hash mismatch")
        sidecar["pin"]["archive_hash"] = before
        for address, code_hash in ((deployment.router, deployment.router_code_hash),
                                   (deployment.datastore, deployment.datastore_code_hash),
                                   (reader_address, reader_code_hash)):
            _code_hash(preflight, address, block, code_hash)
        if (order_handler is None) != (order_handler_code_hash is None):
            raise ValueError("order handler address and code hash must be supplied together")
        if order_handler:
            _code_hash(preflight, order_handler, block, order_handler_code_hash)
        else:
            sidecar["missing_evidence"].append("order_handler_deployment_and_feature_flag")
        nonce = preflight._scalar("getUint(bytes32)", _key("NONCE"), block)
        derived = latest_key(deployment.datastore, nonce)
        pending = preflight._scalar(
            "containsBytes32(bytes32,bytes32)", _key("ORDER_LIST") + _hex_bytes(key, 32), block)
        if pending not in (0, 1):
            raise ValueError("malformed pending membership")
        sidecar["router_gate"] = {"nonce": nonce, "derived_latest_key": derived,
                                  "latest": derived == key, "pending": bool(pending)}
        if derived != key or not pending:
            sidecar["status"] = "ineligible_latest_request"
            return sidecar
        order = _on_chain_order(preflight, key, block)
        mismatches = [name for name, value in request.items() if name in order and
                      (order[name].lower() != value.lower() if isinstance(value, str)
                       else order[name] != value)]
        if mismatches:
            raise ValueError("recorded order differs from pinned order: " + ",".join(mismatches))
        sidecar["pinned_order"] = order
        reader = PinnedArchiveStateReader(traced, deployment)
        subset = reader.read_fixed_increase_subset(
            block=block, expected_hash=before, order_key=key,
            expected_request=order, reader_address=reader_address,
            reader_code_hash=reader_code_hash, index_token=index_token,
            long_token=long_token, short_token=short_token)
        sidecar["position"] = {"key": subset.order_position.position_key,
                               "value": subset.order_position.position}
        sidecar["reader_timestamp_layout"] = subset.order_position.reader_timestamp_layout
        sidecar["fixed_cells"] = [vars(cell) for cell in subset.snapshot.cells]
        sidecar["fixed_values"] = subset.snapshot.values
        sidecar["virtual_inventory"] = {
            "token_id": subset.virtual_token_id,
            "tokens": subset.virtual_inventory_tokens,
            "open_interest_tokens": subset.open_interest_tokens}
        if order_handler:
            feature = reader.read(
                block, before, (execute_order_feature_cell(order_handler, order["orderType"]),))
            sidecar["feature_flag"] = {
                "cell": vars(feature.cells[0]),
                "disabled": feature.values["feature_flags"]["execute_order_disabled"]}
        if (referral_storage is None) != (referral_storage_code_hash is None):
            raise ValueError("referral storage address and hash must be supplied together")
        if referral_storage:
            if not order_handler:
                raise ValueError("referral read requires pinned OrderHandler deployment")
            sidecar["referral"] = read_referral_at_pin(
                traced, deployment, block=block, expected_hash=before,
                account=order["account"], order_handler=order_handler,
                order_handler_code_hash=order_handler_code_hash,
                referral_storage=referral_storage,
                referral_storage_code_hash=referral_storage_code_hash)
            sidecar["missing_evidence"].remove("referral_terms")
            sidecar["missing_evidence"].append("referral_historical_source_and_abi_proof")
        if subset.virtual_token_id is None or subset.virtual_inventory_tokens is None:
            sidecar["missing_evidence"].append("virtual_inventory_semantics")
        sidecar["status"] = "partial_archive_snapshot"
    except (ValueError, OSError, TypeError, KeyError, OverflowError, RpcFailure) as failure:
        sidecar["failure"] = {"type": type(failure).__name__, "reason": str(failure)}
    finally:
        if isinstance(block, int) and block >= 0 and "archive_hash" in sidecar["pin"]:
            try:
                after = _verified_header(preflight, block)
                sidecar["pin"]["hash_after"] = after
                if after != sidecar["pin"]["archive_hash"]:
                    sidecar["status"] = "unavailable"
                    sidecar["failure"] = {"type": "Reorg", "reason": "archive block hash changed"}
            except (ValueError, OSError, TypeError, KeyError, RpcFailure):
                sidecar["status"] = "unavailable"
                sidecar["failure"] = {"type": "ProviderFailure", "reason": "post-read block hash unavailable"}
        sidecar["rpc_transcript"] = traced.calls
        sidecar["rpc_transcript_sha256"] = "0x" + hashlib.sha256(
            json.dumps(traced.calls, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return sidecar
