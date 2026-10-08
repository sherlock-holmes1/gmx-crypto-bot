"""Extract historical Solidity metadata references from saved runtime code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.keys import keccak256

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
