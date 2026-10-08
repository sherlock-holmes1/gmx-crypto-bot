"""Verify saved Sourcify exact-match records against pinned archive bytecode."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.crosscheck.source_manifest import extract_solidity_metadata
from gmx_crypto_bot_v2.domain.keys import keccak256


ROLES = ("router", "datastore", "reader", "order_handler", "referral_storage")


def verify_sourcify_records(manifest_path: Path, records_dir: Path) -> dict[str, Any]:
    """Check public records locally; preserve limits of metadata preimage proof."""
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("schema") != "GmxStep41HistoricalSourceManifest" or \
            set(manifest.get("contracts", {})) != set(ROLES):
        raise ValueError("incomplete historical source manifest")
    verified: dict[str, Any] = {}
    for role in ROLES:
        expected = manifest["contracts"][role]
        raw = (records_dir / f"{role}.json").read_bytes()
        record = json.loads(raw)
        if record.get("runtimeMatch") != "exact_match" or record.get("match") != "exact_match":
            raise ValueError(f"{role} is not an exact Sourcify runtime match")
        if int(record.get("chainId", -1)) != manifest["chain_id"] or \
                record.get("address", "").lower() != expected["address"]:
            raise ValueError(f"{role} Sourcify chain or address mismatch")
        bytecode = record.get("runtimeBytecode", {}).get("onchainBytecode")
        metadata = extract_solidity_metadata(bytecode)
        for field in ("runtime_code_hash", "runtime_code_sha256", "ipfs_cid_v0",
                      "compiler_version"):
            if metadata[field] != expected[field]:
                raise ValueError(f"{role} pinned runtime {field} mismatch")
        compilation = record.get("compilation", {})
        if compilation.get("compilerVersion", "").split("+")[0] != expected["compiler_version"]:
            raise ValueError(f"{role} compilation version mismatch")
        source_metadata = record.get("metadata", {}).get("sources")
        sources = record.get("sources")
        if not isinstance(source_metadata, dict) or not isinstance(sources, dict) or \
                not sources or set(source_metadata) != set(sources):
            raise ValueError(f"{role} source inventory mismatch")
        hashes = {}
        for path, source in sources.items():
            content = source.get("content") if isinstance(source, dict) else None
            if not isinstance(content, str):
                raise ValueError(f"{role} source content missing: {path}")
            actual = "0x" + keccak256(content.encode()).hex()
            source_declared = source.get("keccak256")
            if actual != source_metadata[path].get("keccak256", "").lower() or \
                    (source_declared is not None and actual != source_declared.lower()):
                raise ValueError(f"{role} source hash mismatch: {path}")
            hashes[path] = actual
        abi = record.get("abi")
        if not isinstance(abi, list) or abi != record.get("metadata", {}).get("output", {}).get("abi"):
            raise ValueError(f"{role} ABI mismatch")
        target = compilation.get("fullyQualifiedName")
        if not isinstance(target, str) or ":" not in target:
            raise ValueError(f"{role} compilation target missing")
        target_path, target_name = target.rsplit(":", 1)
        if target_path not in sources or \
                target_name not in record.get("stdJsonOutput", {}).get("contracts", {}).get(target_path, {}):
            raise ValueError(f"{role} compiled target missing")
        verified[role] = {
            "address": expected["address"], "match_id": record.get("matchId"),
            "sourcify_response_sha256": "0x" + hashlib.sha256(raw).hexdigest(),
            "runtime_code_hash": metadata["runtime_code_hash"],
            "runtime_code_sha256": metadata["runtime_code_sha256"],
            "metadata_cid": metadata["ipfs_cid_v0"],
            "compiler_version": metadata["compiler_version"],
            "compilation_target": target,
            "source_keccak256": hashes,
            "abi_entries": len(abi),
            "sourcify_runtime_exact_match": True,
            "local_pinned_code_and_source_hashes_match": True,
            "raw_metadata_cid_preimage_verified": False,
        }
    reader_record = json.loads((records_dir / "reader.json").read_bytes())
    order_source = reader_record["sources"]["contracts/order/Order.sol"]["content"]
    store_source = reader_record["sources"]["contracts/order/OrderStoreUtils.sol"]["content"]
    get_order = [entry for entry in reader_record["abi"] if entry.get("name") == "getOrder"]
    if len(get_order) != 1:
        raise ValueError("Reader.getOrder ABI missing or ambiguous")
    numbers = get_order[0]["outputs"][0]["components"][1]["components"]
    names = [item["name"] for item in numbers]
    if names[9:13] != ["uiFeeFactor", "updatedAtTime", "validFromTime", "srcChainId"] or \
            "uint256 uiFeeFactor;" not in order_source or \
            "keccak256(abi.encode(key, UI_FEE_FACTOR))" not in store_source:
        raise ValueError("historical Reader order layout differs from supported decoder")
    return {
        "schema": "GmxStep41SourcifySourceProof", "version": 1,
        "input_manifest_sha256": "0x" + hashlib.sha256(manifest_raw).hexdigest(),
        "sidecar_sha256": manifest["sidecar_sha256"],
        "chain_id": manifest["chain_id"], "pin_block": manifest["pin_block"],
        "pin_hash": manifest["pin_hash"],
        "contracts": verified,
        "reader_order_numbers": names,
        "reader_ui_fee_slot": 9,
        "reader_updated_at_slot": 10,
        "reader_valid_from_slot": 11,
        "sourcify_exact_match_and_local_content_hashes_verified": True,
        "raw_metadata_cid_preimage_verified": False,
        "independent_compiler_rebuild_verified": False,
    }
