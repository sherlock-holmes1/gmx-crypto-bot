"""Extract historical Solidity metadata references from saved runtime code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.crosscheck.router_preflight import _hex_bytes

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_PREFIX = bytes.fromhex("a2646970667358221220")
_SOLC = bytes.fromhex("64736f6c6343")


def _cid_v0(multihash: bytes) -> str:
    if len(multihash) != 34 or multihash[:2] != b"\x12\x20":
        raise ValueError("unsupported Solidity IPFS multihash")
    integer = int.from_bytes(multihash, "big")
    encoded = ""
    while integer:
        integer, digit = divmod(integer, 58)
        encoded = _ALPHABET[digit] + encoded
    return encoded


def _cid_digest(cid: str) -> bytes:
    if not isinstance(cid, str) or not cid.startswith("Qm"):
        raise ValueError("invalid CIDv0")
    value = 0
    for character in cid:
        if character not in _ALPHABET:
            raise ValueError("invalid CIDv0 alphabet")
        value = value * 58 + _ALPHABET.index(character)
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if len(raw) != 34 or raw[:2] != b"\x12\x20" or _cid_v0(raw) != cid:
        raise ValueError("invalid CIDv0 SHA-256 multihash")
    return raw[2:]


def extract_solidity_metadata(code_hex: str) -> dict[str, Any]:
    """Decode only the exact IPFS+solc trailer seen in the pinned deployment."""
    if not isinstance(code_hex, str) or not code_hex.startswith("0x"):
        raise ValueError("missing historical runtime code")
    code = bytes.fromhex(code_hex[2:])
    if len(code) < 53:
        raise ValueError("short historical runtime code")
    size = int.from_bytes(code[-2:], "big")
    if size != 51 or size + 2 >= len(code):
        raise ValueError("unsupported Solidity metadata trailer length")
    trailer = code[-size-2:-2]
    if not trailer.startswith(_PREFIX) or trailer[42:48] != _SOLC:
        raise ValueError("unsupported Solidity metadata trailer layout")
    multihash = trailer[8:42]
    compiler = trailer[48:51]
    return {"ipfs_cid_v0": _cid_v0(multihash),
            "compiler_version": ".".join(str(x) for x in compiler),
            "metadata_length": size,
            "runtime_code_hash": "0x" + keccak256(code).hex(),
            "runtime_code_sha256": "0x" + hashlib.sha256(code).hexdigest(),
            "runtime_code_bytes": len(code)}


def build_source_manifest(sidecar_path: Path) -> dict[str, Any]:
    """Bind saved code bytes to address/hash/CID without fetching a source."""
    raw = sidecar_path.read_bytes()
    sidecar = json.loads(raw)
    if sidecar.get("schema") != "GmxStep41ArchiveSidecar" or sidecar.get("version") != 1:
        raise ValueError("unsupported archive sidecar")
    deployment = sidecar["deployment"]
    if sidecar.get("pin", {}).get("archive_hash") != sidecar.get("pin", {}).get("hash_after"):
        raise ValueError("sidecar pin hash not stable")
    roles: dict[str, tuple[str, str]] = {}
    for name, address, code_hash in (
        ("router", "router", "router_code_hash"),
        ("datastore", "datastore", "datastore_code_hash"),
        ("reader", "reader", "reader_code_hash"),
        ("order_handler", "order_handler", "order_handler_code_hash"),
        ("referral_storage", "referral_storage", "referral_storage_code_hash"),
    ):
        if not deployment.get(address) or not deployment.get(code_hash):
            raise ValueError(f"missing {name} deployment address or code hash")
        roles[name] = (deployment[address], deployment[code_hash])
    observed: dict[str, dict[str, Any]] = {}
    pin = sidecar["pin"]["number"]
    for call in sidecar.get("rpc_transcript", []):
        if call.get("method") != "eth_getCode":
            continue
        params = call.get("params")
        if not isinstance(params, list) or len(params) != 2 or params[1] != hex(pin):
            raise ValueError("unbound code read in sidecar")
        address = params[0].lower()
        code = call.get("response", {}).get("result")
        metadata = extract_solidity_metadata(code)
        if address in observed and observed[address]["runtime_code_hash"] != metadata["runtime_code_hash"]:
            raise ValueError("conflicting runtime code for one address")
        observed[address] = metadata
    contracts = {}
    for role, (address, expected_hash) in roles.items():
        metadata = observed.get(address.lower())
        if metadata is None or metadata["runtime_code_hash"] != expected_hash.lower():
            raise ValueError(f"missing or mismatched {role} runtime code")
        contracts[role] = {"address": address.lower(), **metadata,
                           "metadata_uri": "ipfs://" + metadata["ipfs_cid_v0"],
                           "source_status": "metadata_reference_only_not_fetched"}
    return {"schema": "GmxStep41HistoricalSourceManifest", "version": 1,
            "sidecar_sha256": "0x" + hashlib.sha256(raw).hexdigest(),
            "chain_id": deployment["chain_id"],
            "pin_block": pin, "pin_hash": sidecar["pin"]["archive_hash"],
            "contracts": contracts,
            "historical_source_abi_key_layout_proved": False,
            "next_proof": "fetch each content-addressed metadata and source, verify hashes and compiler output against pinned runtime code"}


def verify_local_source_bundle(manifest: dict[str, Any], bundle_root: Path) -> dict[str, Any]:
    """Hash-check downloaded compiler metadata and every referenced source file.

    Files belong under `<root>/<role>/metadata.json` and
    `<root>/<role>/sources/<source path>`. This verifies content references,
    not the compiled runtime or historical key semantics.
    """
    if manifest.get("schema") != "GmxStep41HistoricalSourceManifest" or manifest.get("version") != 1:
        raise ValueError("unsupported source manifest")
    expected_roles = {"router", "datastore", "reader", "order_handler", "referral_storage"}
    if set(manifest.get("contracts", {})) != expected_roles:
        raise ValueError("incomplete source manifest contracts")
    _hex_bytes(manifest.get("sidecar_sha256"), 32)
    _hex_bytes(manifest.get("pin_hash"), 32)
    if type(manifest.get("chain_id")) is not int or manifest["chain_id"] <= 0 or \
            type(manifest.get("pin_block")) is not int or manifest["pin_block"] < 0:
        raise ValueError("missing source manifest chain or pin identity")
    for role, contract in manifest["contracts"].items():
        _hex_bytes(contract.get("address"), 20)
        _hex_bytes(contract.get("runtime_code_hash"), 32)
        _hex_bytes(contract.get("runtime_code_sha256"), 32)
    manifest_digest = "0x" + hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    resolved_bundle = bundle_root.resolve()
    reports: dict[str, Any] = {}
    for role, contract in manifest["contracts"].items():
        role_root = (resolved_bundle / role).resolve()
        if not role_root.is_relative_to(resolved_bundle):
            raise ValueError(f"{role} role path escapes bundle")
        metadata_path = role_root / "metadata.json"
        if not metadata_path.resolve().is_relative_to(role_root):
            raise ValueError(f"{role} metadata path escapes role")
        raw = metadata_path.read_bytes()
        if hashlib.sha256(raw).digest() != _cid_digest(contract["ipfs_cid_v0"]):
            raise ValueError(f"{role} metadata CID hash mismatch")
        metadata = json.loads(raw)
        version = metadata.get("compiler", {}).get("version", "").split("+")[0]
        if version != contract["compiler_version"]:
            raise ValueError(f"{role} compiler version mismatch")
        sources = metadata.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise ValueError(f"{role} metadata has no source hashes")
        if not isinstance(metadata.get("output", {}).get("abi"), list):
            raise ValueError(f"{role} metadata ABI missing")
        source_hashes = {}
        sources_root = (role_root / "sources").resolve()
        if not sources_root.is_relative_to(role_root):
            raise ValueError(f"{role} sources directory escapes role")
        for source_path, source_meta in sources.items():
            source = (sources_root / source_path).resolve()
            if not source.is_relative_to(sources_root):
                raise ValueError(f"{role} source path escapes bundle")
            content = source.read_bytes()
            actual = "0x" + keccak256(content).hex()
            if actual != source_meta.get("keccak256", "").lower():
                raise ValueError(f"{role} source hash mismatch: {source_path}")
            source_hashes[source_path] = actual
        reports[role] = {"address": contract.get("address"),
                         "runtime_code_hash": contract.get("runtime_code_hash"),
                         "runtime_code_sha256": contract.get("runtime_code_sha256"),
                         "metadata_cid": contract["ipfs_cid_v0"],
                         "metadata_sha256": "0x" + hashlib.sha256(raw).hexdigest(),
                         "compiler_version": version, "source_keccak256": source_hashes,
                         "abi_entries": len(metadata["output"]["abi"])}
    return {"schema": "GmxStep41SourceBundleVerification", "version": 1,
            "input_manifest_sha256": manifest_digest,
            "sidecar_sha256": manifest.get("sidecar_sha256"),
            "chain_id": manifest.get("chain_id"),
            "pin_block": manifest.get("pin_block"),
            "pin_hash": manifest.get("pin_hash"),
            "content_hashes_verified": True,
            "runtime_rebuild_verified": False,
            "historical_key_layout_reviewed": False,
            "contracts": reports}
